# 把"改进机制"做成可执行对象：`brian evolve`

> 两个来源：
> [The Path to Recursive Self-Improving Agents: Foundation, Framework, and Future Directions](https://github.com/D2I-ai/awesome-recursive-self-improving-agents)（Alibaba / D2I.ai，Liu / Lin / Zhang / Ren / Wu / Li / Wang / Fu / Ye）
> [Recursive Self-Improvement in AI: From Bounded Self-Refinement to Autonomous Research Loops](https://arxiv.org/html/2607.07663v2)（arXiv:2607.07663v2，Chen / Wang / Qu，综述 1,250 篇）

本文只记录**落地到本仓库**的部分、实测数字，以及做不到的地方。

---

## 1. 两个来源里能被施工的那几条

### 1.1 形式化（D2I.ai）

一个 agent 系统写成

```
x_t = (M_t, H_t, D_t, T_t, Imp_t)      # 模型、harness、数据系统、训练器、改进机制
x_{t+1} = Imp_t(x_t)
```

当 `Imp_t` 自己也能被改时，才叫 RSI。`Imp` 内部固定是四步：

```
诊断瓶颈 → 提出候选修改 → 评测并选择 → 把接受的改动合并进持久状态
```

L1–L5 分级：L1 手工；L2 辅助（机器提议、人来验证/应用）；L3 程序化自改进（提议+实现+验证全自动，`Imp` 固定）；L4 有界 RSI（`Imp` 可变）；L5 通用 RSI（再加跨域迁移）。

### 1.2 纪律（arXiv:2607.07663v2）

- **验证层级**：形式验证器 > 外部测试/标注数据 > 确定性测量 > 模型自评。"没有外部信号，就没有可靠的改进。"
- **失败模式**：自证循环、模型坍缩、多样性坍缩。
- **科学失忆**：行为在一轮轮"战役"里变好，但方法论知识没有沉淀。
- **技能库的反面证据**：SkillsBench 中人造技能 +16.2 通过率百分点，**LLM 自造技能 +0**。
- 这篇综述认为工程师真正要造的工件是**循环**（触发、目标、验证、停止条件、记忆），不是某一步的提示词。

### 1.3 对照本仓库

| 形式化环节 | 本仓库原来有没有 |
|---|---|
| 诊断 | **有**（`brian cost` 的账本，第 1 轮补的） |
| 提出候选 | **没有**（前十轮都是 agent 手工想、手工改、手工提交） |
| 评测并选择 | **没有**（评测器有，但没有"接不接受"的判据） |
| 合并进持久状态 | **没有**（`always_analyze_alerts` / `analyze_actions` / `reuse_report_within_s` 连环境变量都没接，改完只能改代码） |

所以前十轮 RSI Tax 优化在阶梯上是 **L1**：机器没有参与提议，也没有参与裁决。这正是"改进机制缺失"的具体形状。`src/brian/evolve.py` 就是补这个洞。

---

## 2. 落地结构

### 2.1 五步对应到代码

| 步骤 | 代码 |
|---|---|
| 诊断 | `CostLedger`（`brian cost`），循环自己不重新定义"哪里贵" |
| 提出 | `catalogue()`：9 个候选，每个必须带一个**可被证伪的假设**；候选是一份补丁（可含多个设置项） |
| 评测 | `measure()`：标注数据（recall/FPR，按记录各取切点）+ 证据覆盖（确定性）+ token 账本，三级信号 |
| 选择 | `decide()`：先硬否决，再要求 Pareto 增益（成本或检测，任一方向，另一方向不退） |
| 合并 | `persist()`：`var/evolve/versions/v###.json`，内含可复现的环境变量与 `unset` 回滚指令 |

命令行：

```
python -m brian.cli evolve                          # 跑一轮，记录，打表
python -m brian.cli evolve --apply                  # 另外把接受项落成版本文件
python -m brian.cli evolve --history                # 历史轮次
python -m brian.cli evolve --grade                  # 在 L1–L5 上的位置与缺口
python -m brian.cli evolve --stream-file data/rcaeval/stream.json   # 换真实数据
```

每一轮都会被记录（`var/evolve/round-*.json` + `ledger.jsonl`），**包括什么都没接受、什么都没应用的轮次**。
只有"接受"的轮次才留记录，等于让下一轮重新提出一个已经有结论的问题——那正是综述点名的"科学失忆"：
行为在一轮轮战役里变好，方法论知识没有沉淀。`--apply` 只控制要不要额外写版本文件，
因为"判定接受"和"合并进部署"是两个事实，分级也分开评（见第 4 节）。

`--stream-file` 是第 2 轮加的：流量来自 `brian.stream/1` 文件（`src/brian/streams.py`，只依赖标准库），
由 `scripts/service_data/rcaeval_stream.py` 与 `scripts/real_data/smd_stream.py` 一次性生成。
一条流 = 一段录像，**各自带自己的基线**——把 A 录像是历史喂给 B 录像的第一个窗口，测的是两段录像的接缝，
不是检测器，和"多台机器共用一个基线"是同一个错误。

文件里的 `overrides` 不是便利功能：出厂阈值是在本仓库的合成域上校的，原封不动挪到另一个域会把检测器整个静音，
而那一轮的报告会写"没有候选有帮助"——把一个配置错误说成科学结论。把基础配置放进数据文件，是让这个选择
随数据一起走、出现在记录里、下一个跑循环的人不可能忘记。`apply_overrides()` 在任何候选被测量之前应用它，
所以每个候选都站在同一个基线上。

### 2.2 三级信号为什么是这个顺序

1. **标注数据**（最强）：recall / FPR。任何回退直接否决，与省钱与否无关。
2. **证据覆盖**（中等）：`select_events` 截断后，日志行、不同错误签名、首个错误、部署/配置标记各保留多少。没有配置模型时，解释质量没法测，但"提示词被饿死"可以测。
3. **token 账本**（客观但最弱）：只有在 1、2 都过了之后才看。

顺序就是验证层级，照抄。反过来先看省钱，会接受一个"便宜但瞎"的系统。

### 2.3 `veto`：被指标看不见的否决

`analyze-pages-only` 的实测是**最好看的**：token −37.4%，recall/FPR 一点不变。纯数字循环必然接受它。但它删掉的正是 `deep_analysis` 这个动作存在的理由（"不确定，需要更强推理"），等于把动作阶梯的一半拆了。所以候选自带 `veto` 字段，把"这个改动不能只看指标"写成数据，而不是留在评审者的脑子里。

### 2.4 环境变量面：合并的最后一公里

新加的 `ENV_VARS`（`src/brian/config.py`）是 `(section, field) → 变量名` 的显式表，`tests/test_config_env.py` 把表里每一项都真过一遍 `load_settings`：

- 一条命名错位的版本文件比没有版本文件更坏——它记录了改动、运维照做了、什么都没发生。
- 顺带补上了 `BRIAN_GATE_ALWAYS_ANALYZE` / `BRIAN_GATE_ANALYZE_ACTIONS` / `BRIAN_GATE_REUSE_REPORT_S`：这三个开关原来只存在于构造函数默认值和 CLI 参数里，**部署时无法设置**。

另外 `pyproject.toml` 补了 `pythonpath = ["src"]`：`src/` 布局下，不装包就跑不了测试，"测试通过"不该依赖仓库之外的状态。

---

## 3. 一次真实测量

命令：`python -m brian.cli evolve`（合成流，35 窗口 / 15 个计分窗口 / 8 个异常 / 2,565 事件；reasoner = `offline`，无 API key，token 为提示词实测估算，取 `CHARS_PER_TOKEN = 3.0`）。

| 候选 | recall | FPR | token/报告 | token/窗口 | 证据 | 判定 |
|---|---|---|---|---|---|---|
| baseline | 0.875 | 0.000 | 4,875 | 1,114.3 | 100% | — |
| `events-8` | 0.875 | 0.000 | 3,406 | 778.6 | 76% | 拒绝（证据） |
| `events-4` | 0.875 | 0.000 | 2,929 | 669.4 | 40% | 拒绝（证据） |
| `reuse-300` | 0.875 | 0.000 | 3,686 | 842.5 | 100% | **接受（−24.4%）** |
| `analyze-pages-only` | 0.875 | 0.000 | 4,875 | 1,114.3 | 100% | 拒绝（否决） |
| `page-severity-3` | 0.875 | 0.000 | 4,899 | 1,119.8 | 100% | 拒绝（无增益） |
| `confidence-auto-85` | 0.875 | 0.000 | 4,875 | 1,114.3 | 100% | 拒绝（无增益） |

整轮 31 秒。接受项落成 `var/evolve/versions/v001.json`：

```
export BRIAN_GATE_REUSE_REPORT_S=300
# rollback: unset BRIAN_GATE_REUSE_REPORT_S   （恢复默认 0，即不复用）
```

### 3.1 循环与手工轮次的分歧（这一节是重点）

第 4 轮的结论是"8 个事件保留 **100%** 的日志行和 **100%** 的不同签名，省钱 31%，零信息损失"，并据此给了默认值建议。循环给出的却是 **76% / 85%，否决**。

不是循环错了。第 4 轮的分母是 `select_events(history_20, 8)` 对 `history_20`——也就是问"在已经只读 20 条的提示词里，选择器是否比朴素截断更聪明"。那问题的答案确实是 100%。但那是**同一预算内**的比较，它已经默认接受了从 64 条历史压到 20 条这件事。

循环问的是另一个更严格的问题："相对当前生产提示词（20 条，覆盖 1.00），8 条丢不丢信息？"以 64 条参考历史为分母，答案是不光丢，而且丢的是**不同错误签名**——签名的种类，不是重复的次数。省下来的 31% 是真的，损失也是真的。

这就是需要外部验证信号的直接证据：手工轮次里，"100%"这个数字本身没错，错的是它的分母，而分母是写脚本的人选的。循环把分母固定在候选之外，候选就无法通过缩小分母来给自己加分。

### 3.2 另一条被数据推翻的判断

第 5 轮结论"按事故类别做策略不是杠杆"（各类每报告 3,807–4,072，全部 0.9–1.0× 中位数）在循环里复现了：`page-severity-3`、`confidence-auto-85` 既不改善检测也不改善成本，被"无增益"规则正确拒绝。一个只会在自己身上找收益的循环，至少还能证明某些杠杆确实是零。

### 3.3 换一条流，结论不变

同一条命令换序列与种子（`normal,security,security,normal,data_quality,data_quality,normal,config_or_deploy,normal`，seed 19，9 个计分窗口，recall 1.000）：

| 候选 | token/报告 | 证据 | 判定 |
|---|---|---|---|
| baseline | 4,880 | 100% | — |
| `events-8` | — | 88% / 96% | 拒绝（证据） |
| `events-4` | 2,971 | 47% / 75% | 拒绝（证据） |
| `reuse-300` | 3,019 | 100% | **接受（−38.1%）** |
| `analyze-pages-only` | 4,880 | 100% | 拒绝（否决） |
| `page-severity-3` | 4,896 | 100% | 拒绝（无增益） |
| `confidence-auto-85` | 4,880 | 100% | 拒绝（无增益） |

两条流上唯一的接受项都是报告复用；截断提示词的候选在两条流上都丢签名，且第二条流的 `events-8` 只丢 4% 的签名、仍然被拒绝——因为规则是"任何一列不许回退"，不是"平均起来差不多"。接受幅度随流而变（−24.4% / −38.1%）恰好说明复用省的钱取决于重复率，不是常数。

### 3.4 真实数据（RCAEval）：循环拒绝在无法验证的地方接受

合成流有个结构性缺陷：检测器在上面 recall 0.875、FPR 0.000，标签这一列是饱和的，**任何**候选都能通过。一个永远不会拒绝的信号不是信号。所以第二步是把循环指向没有人手写的真实数据。

适配器 `scripts/service_data/rcaeval_stream.py` 把注入录像一次性转成流文件（格式 `brian.stream/1`，`src/brian/streams.py` 只依赖标准库读回），于是循环本身不需要 parquet 栈。`--per-fault 3` 取 cpu/delay/disk/loss/mem 各 3 段：

```
$ python -m brian.cli evolve --stream-file data\rcaeval\stream.json
base override   calibration: enabled=False
base override   gate: anomaly_probability=0.5, notify_probability=0.5
base override   window: min_events=1
evolving over 15 recording(s), 543 windows (456 scored, 279 anomalous, 2042 events)
```

| 候选 | recall | fpr | token/报告 | 证据 | 判定 |
|---|---|---|---|---|---|
| baseline | 0.000 | 0.000 | n/a | 100% | — |
| `events-8` | 0.000 | 0.000 | n/a | 100% | 拒绝（无报告） |
| `events-4` | 0.000 | 0.000 | n/a | 100% | 拒绝（无报告） |
| `reuse-300` | 0.000 | 0.000 | n/a | 100% | 拒绝（无报告） |
| `analyze-pages-only` | 0.000 | 0.000 | n/a | 100% | 拒绝（否决） |
| `page-severity-3` | 0.000 | 0.000 | n/a | 100% | 拒绝（无报告） |
| `confidence-auto-85` | 0.000 | 0.000 | n/a | 100% | 拒绝（无报告） |
| `anomaly-p-40` | 0.000 | 0.000 | n/a | 100% | 拒绝（无报告） |

基线在这条域上**一条告警都没有**：出厂阈值 `anomaly_probability=0.5` 下 279 个故障窗口全是 `Verdict.NORMAL`。这不是新发现——服务数据那一轮已经测过这台引擎在 RCAEval 上是抛硬币（AUC 0.44–0.51，5 个故障族全在 0.5 以下）。新的是循环的反应：它没有"因为测不到就接受"，而是给出 `no-reports` 判定——"baseline 一次报告都没产生，token/报告无从谈起，成本比较需要两侧至少各有一条告警"。

为此 `decide()` 增加了一条 `no-reports` 规则，`format_round()` 把 `None` 渲染成 `n/a`。这两个改动很小，但它们把"测不到"和"有增益"分开了：在这个改动之前，`tokens_per_report is None` 是一个 `TypeError`。

这一轮被记录了（`var/evolve/round-20261117T183601Z.json`），`--history` 里是第 2 行：

```
 1  2026-11-17T17:57:34+00:00  accepted=reuse-300  tokens/report  4875 -> 3686
 2  2026-11-17T18:39:45+00:00  accepted=-          tokens/report   n/a -> (none accepted)  [recorded, not applied]
```

"记录一个什么都没接受的轮次"本身就是对科学失忆的修复：下一轮会在 `Previously rejected` 里看到这七条结论，不会再把它们当成新想法重新提一遍。

### 3.5 域看起来不可检测时，先怀疑接线

SMD 主机监控是第二条真实数据。第一版跑出来的结果荒唐但值得记录：**120 个窗口只有一个不同的 digest**，`laya_cache_hits=119/120`，循环报告"没有报告产生"。看起来像"这个域检测不出来"，实际是循环在测一个自己没读的域。三个 bug，每个都足以让真实数据看起来不可检测：

1. `Windower(...)` 不传 `specs` 就用 service 目录，host 的 payload key（`cpu_r`/`load_1`/`mem_free`…）全被丢掉，`digest.metrics` 里没有 `host.cpu`。
2. `build_digest(aggregator, baselines)` 默认 `profile="service"`，host 渲染器根本没跑。
3. `BaselineTracker` 默认 service 的 `BASELINE_KEYS`：`observe()` 按 key 在窗口里找不到值，什么也不追加，于是每个窗口都渲染 `No baseline is available yet`——所有请求文本相同，Laya 的缓存直接把第一个答案复用了 119 次。

修法是把 profile 变成设置项，三者一起走：`DigestSettings.profile`（`src/brian/config.py`，`BRIAN_DIGEST_PROFILE`）、`digest_specs(profile)` 与 `baseline_keys(profile)`（`src/brian/state.py`，未知名字抛 `ValueError` 而不是返回空元组），`Pipeline` 与 `Evaluator` 都按 profile 取目录、取基线 key、渲染。修完之后 240 个窗口给出 238 个不同解释，缓存命中降到 2。

这条 bug 链的教训是方法论上的：**一个"域不可检测"的结论，在接线被验证过之前不算结论**。`digest_specs` 与 `baseline_keys` 的测试就是为此存在的——目录、基线 key、渲染器必须一起动，只改其中一个会让整个域静音。

### 3.6 一次被推翻的判断：0.745 不是 bench 独有，稀释来自特征数

第 2 轮结束时曾写过这样一句：第 1–6 轮反复引用的 SMD AUC 0.745 来自 `scripts/real_data/bench_smd.py` **自己的渲染器**，不是出厂的 `render_digest(profile="host")`。**这句话是错的**，而且错得很容易发生，值得留着。

读 `bench_smd.py` 的 `render()` 就能确认：`--digest product` 这一支的输入就是 `build_digest(..., profile="host")` 的 `digest.interpretation`，即出厂渲染器的原文。`data/smd/bench_*.json` 里共 52 份历史结果，按 AUC 排序，头部全是这一支：

| 结果文件 | digest | 特征数 | AUC | recall@10%FPR |
| --- | --- | --- | --- | --- |
| `bench_current_h100000.json` | current | 3 | 0.7721 | 0.4089 |
| `bench_current_h480.json` | current | 3 | 0.7577 | 0.4089 |
| `bench_product_prod480_fixed.json` | **product** | 3 | 0.7558 | 0.4141 |
| `bench_product_prod_full.json` | **product** | 3 | 0.7501 | 0.3864 |
| `bench_product_r4_w600.json` | **product** | 3 | 0.7447 | 0.3801 |
| `bench_level_range_r1.json` | level_range | 3 | 0.4107 | 0.0455 |

同一个渲染器，3 个特征 0.7501，而第 2 轮用全部 8 个特征测出来是 0.4176。差别不在渲染器，在**给渲染器几路信号**：`--features cpu_r,load_1,mem_free` 与出厂 `HOST_METRICS` 的全部八列是两份不同的模型输入，而后者把三列里的可分性摊薄到了噪声里。`level_range` 反而是最差的一支（0.4107），说明"换个更好看的措辞"不是杠杆，"少喂几路互相无关的指标"才是。

`bench_product_prod_full.json` 还顺带解释了后面所有"检测不出来"的现象。4708 个窗口的答案分布是：异常中位数 0.323、最大 0.414，正常中位数 0.297、最大 0.467，10% FPR 的切点在 **0.3304**。也就是说引擎在这个域上的全部回答都挤在 0.30–0.47，而出厂 `anomaly_probability=0.5`（默认 0.5583）比其中最高的答案还高——不是检测器看不见，是门限把整个域静音了。第 2 轮在 0.20–0.50 之间扫切点只看到"0 个告警"，原因在此。

### 3.7 用生产路径复现：0.674 对 0.511，以及"两个主机不共享一把尺"

`var/diag_host3.py` 不动 bench，直接跑 `Evaluator` + `apply_overrides`，只换 `digest.host_features`：

| 记录 | 特征 | AUC | recall@10%FPR | 切点 |
| --- | --- | --- | --- | --- |
| machine-1-6 | `cpu_r,load_1,mem_free` | **0.6740** | 0.282 | 0.3231 |
| machine-1-6 | 全部 8 列 | 0.5114 | 0.090 | 0.3786 |
| machine-2-2 | `cpu_r,load_1,mem_free` | **0.3467** | 0.054 | 0.3505 |

机器 1-6 上，特征稀释在生产路径里同样复现：0.674 对 0.511。但机器 2-2 用同样三列只有 0.3467，**低于机会线**——它的 digest 值本身接近零（`CPU 0.02 (baseline 0.00)`、`load 1m 0.01 (baseline 0.00, 3.60x)`），三列里没有可读的变化。所以"三特征更好"是机器 1-6 的结论，不是 SMD 的结论。

这件事接着暴露了循环本身的一个错误：第一版 `operating_point` 把所有记录的 `predicted_probability` 拼在一起扫一个切点。两台主机的答案根本不在同一刻度上（1-6 挤在 0.28–0.35，2-2 在 0.30–0.35 但方向相反），合并扫出来的 `recall@10% = 0.030` 既不是 1-6 的 0.282 也不是 2-2 的 0.054，而是把一个主机的切点套到另一个主机的负例上。现在的 `operating_point_by_recording(groups, budget)` 对每条记录各扫各的切点、再把 tp/fp 相加——这正是部署时按主机分别标定阈值做的事；某条记录没有正例或没有健康窗口时无法扫，就退回合并扫描并在结果里标 `pooled: true`，而不是把它丢掉（丢掉会藏起它的误报）。

教训与 3.5 同类，但方向相反：3.5 是"接线错了会显得域不可检测"，这里是"**指标合并错了会显得检测器更差**"。两者都只有在把测量拆开看的时候才看得见。

### 3.8 只有成本一个出口，循环会把稀释的检测器永久冻结

`decide()` 原来的接受条件只有一条：tokens/report 下降 ≥5%。这在 SMD 上意味着 `host-features-3`（检测 AUC 0.511 → 0.674，成本不变）会被判"低于门槛"而拒绝——一个以省钱为目标的循环，会把一个因为省钱而变哑的检测器永远留在原地。

现在有两条接受路径，都要求另一列不退步：

- **cost**：tokens/report 降 ≥5%，recall@budget、FPR@budget、证据覆盖都不退；
- **quality**：recall@budget 升 ≥5%，tokens/report 上涨不超过 5%；两侧都没有报告（该域根本不告警）时视为成本无异议——否则循环在最需要它的域上反而无法决定。

改完之后又发现自己写错了第二处：**预算内的误报率不是要最小化的分数，是许可证**。第一版在 at-budget 分支里要求 `fpr_at_budget` 不得上升，于是同一轮里 `host-features-3` 的 recall@10% 从 **0.060** 升到 **0.187**（三倍），却因为误报率从 **0.046** 升到 **0.089**（仍在 10% 预算内）被判 `fpr` 拒绝。真正该拒绝的是两件事：超过预算（现在是硬否决），以及**多花了预算却没多捞到故障**（仍是硬否决，因为那是别人要接的页面）。多花预算换到三倍告警召回，是交易，不是回退——表格里每一行都印着实际花掉的比率，读者能自己判断这笔交易值不值。

### 3.9 第三道门限：`notify_probability` 让成本列整列为空

第 2、3 轮在 SMD 上都出现同一个现象：`tokens/report` 是 `null`，七个候选全部以 `no-reports` 被拒绝。原因不是"这个域不告警"，而是**门槛三件套里的第三件**：

```python
# src/brian/laya/gate.py:195-205
if is_anomaly:
    if anomaly_probability < thresholds.notify_probability:
        action = DecisionAction.OBSERVE          # recorded, not raised
```

`scripts/real_data/smd_stream.py` 的 `BASE_OVERRIDES` 里 `anomaly_probability=0.33`、`notify_probability=0.5`，而引擎在这个域上的回答全部落在 0.30–0.47（3.6 节的分布），于是每个窗口都是"异常但不够格通知" → `OBSERVE` → 不在 `ALERTING_ACTIONS` 里 → 不产生告警 → System 2 不跑 → 账本为空。`Evaluator` 只读 `decision.is_anomaly`，所以标签列照常有数，成本列却是空的。

这和 3.5 节的三条 bug 是同一类，只是位置更靠后：**检测、告警、计费是三件事**，只把检测修好，循环依然拿不到成本信号。修法是让两条门限在这个域上同高（`notify_probability=0.33`），因为 0.5 这个值是从 service 域继承来的，在 host 域上没有依据。这条修改属于"让这个域能跑起来"的接线，不属于候选。

### 3.10 第一次在真实数据上有候选被接受

接线补齐后重跑同一条流（948 个窗口、828 个计分、134 个异常、4740 个事件，记录 `var/evolve/round-20261117T193010Z.json`）：

| 候选 | recall@10% | fpr@10% | tok/report | tok/window | 证据 | 判决 |
|---|---|---|---|---|---|---|
| baseline | 0.060 | 0.046 | 3349 | 3327.5 | 100% | — |
| `events-8` | 0.060 | 0.046 | 2438 | 2422.7 | 100% | **接受 −27.2%** |
| `events-4` | 0.060 | 0.046 | 2135 | 2121.2 | 100% | **接受 −36.3%** |
| `reuse-300` | 0.060 | 0.046 | 3349 | 3327.5 | 100% | 拒绝（无重复告警可复用，两条都不动） |
| `analyze-pages-only` | 0.060 | 0.046 | 3349 | 3327.5 | 100% | 拒绝（veto） |
| `page-severity-3` | 0.060 | 0.046 | 3357 | 3336.1 | 100% | 拒绝（成本上升 0.2%，召回不动） |
| `confidence-auto-85` | 0.060 | 0.046 | 3349 | 3327.5 | 100% | 拒绝（两条都不动） |
| `anomaly-p-40` | 0.060 | 0.046 | 3323 | 73.6 | 100% | 拒绝（成本 −0.8%，未过 5%） |
| `host-features-3` | **0.187** | 0.089 | 2887 | 1108.6 | 100% | **接受（质量，3.1× 召回）** |

三处值得记下的东西。

**一、`host-features-3` 是这条流上唯一被接受的检测改动**，也是唯一同时动了两列的候选：它单独就把 tokens/window 从 3327.5 压到 1108.6（−67%），因为三层特征让引擎的答案整体落在 0.28–0.35 这一段（3.7 节 AUC 0.674 对 0.511），而更低的分数意味着 prompt 里更短的数字。但因果要说清：**是特征选择让引擎答得更有区分度，压缩只是副作用**——不许把它写成"压缩带来了更好的检测"。

**二、`events-8`/`events-4` 在 SMD 上被接受，而同一候选在前两条流上因证据不足被拒。** 这不是规则前后不一致，是证据列在这条流上**结构性失效**：SMD 的每个窗口只有 `metric` 一种事件、payload 只有一组 8 个键，没有任何日志、签名或 context 标记（`var/pool_coverage.py` 实测：cap 64/20/8/4 全部 1.000）。防护栏只在有日志可丢的流上才会响。检测列之所以纹丝不动，是因为 Laya 读的是 digest，而 digest 由窗口自己的 5 个样本算出，`history` 只进 System 2 的 prompt——把 history 从 64 砍到 8，动的是解释材料，不是判别依据。**结论：这两个接受只由成本列支撑，解释质量是否下降在本域无法测量（无 API key）。**

**三、预算内的误报率是许可证，不是分数。** 第一版规则要求 `fpr@budget` 不得上升，于是 `host-features-3` 在同一轮里被 `fpr` 拒绝：召回 0.060 → 0.187（三倍），误报 0.046 → 0.089（仍在 10% 预算内）。改后的规则只拒绝两种情况：**超过预算**，以及**多花了预算却没多捞到故障**——那是要人接的页面。多花 4.3 个百分点的预算换三倍召回是交易，表格里每行都印着实际花掉的比率，值不值由读者判断。

### 3.11 状态上限：同一段 digest，简单读者 0.827 / 0.597，引擎 0.674 / 0.347

3.10 节的表能回答"哪个配置更省"，但回答不了"如果标签列本身已经到顶，循环还能改什么"。所以每轮现在多印一块 `state ceiling`：**同一段渲染文本，一个什么都不用、只会取最大偏离的读者能恢复多少标签**。实现是 `src/brian/evolve.py` 的 `state_ceiling()`——重放 `Windower → build_digest → observe`，对每个窗口取渲染器实际会印出的那些行的 `max |ratio − 1|`（优先长基线，因为宿主渲染器优先它），跟标签算 AUC，**不调用 Laya、不花一个 token，948 个窗口 0.2 秒**。

| 记录 | 窗口 | 简单读者（本仓库生产实现） | 引擎（同一段文本） | 结论 |
|---|---|---|---|---|
| `smd-machine-1-6` | 474 | **0.827** | 0.674 | 文本里有信号，引擎只抓到一半 |
| `smd-machine-2-2` | 474 | **0.597** | 0.347 | 文本里仍有信号，引擎给出反向排序 |
| 合成 `demo` | 35 | **0.995** | — | 合成流几乎可以被一条阈值完美分开 |

三点必须说清。

**一、0.827 不是新发现，是更正。** 3.7 节那个 0.674 是引擎读 3 特征 digest 的 AUC；同一批窗口用正则从那几行 `Nx` 里取最大偏离，得到 0.796（1-6）和 0.589（2-2）。本节的 0.827 / 0.597 略高，原因只有一个：`state_ceiling()` 补上了 `windower.flush()` 的最后一个窗口，并从 `MetricValue` 结构里直接读 `long_delta_ratio`，而不是从渲染好的文本里用正则抠数字。**两个版本的差别是测量误差，不是域的性质**——这也说明为什么应当让生产路径来算这个数，而不是再写一个脚本。

**二、一个被实验否掉的假设。** 解释 2-2 低于机会线的直觉假设是"引擎带有人类那种内存压力先验：空闲内存越低越异常"。用两段只差在空闲内存方向的 digest（`0.83x` 对 `1.39x`，主机名写在文本里）直接问引擎：2-2 得到 0.2421（更低）对 **0.2644**（更高），1-6 得到 0.2453 对 **0.2687**。**空闲内存更高在两个主机上都得分更高**，所以方向先验不存在；同时这两个数（0.24–0.27）说明引擎在宿主 digest 上的绝对水平本来就没有信心，与它在 SMD 流上给出的 0.28–0.35 是同一量级。

**三、"简单读者"继承基线追踪器的盲区，而不是隐藏它。** 单元测试里用持续阶跃（异常是最后 8 个窗口连续高）构造 fixture，上限只有 **0.8** 而不是 1.0：滚动中位数会跟随一个比窗口更长的故障，`max |ratio − 1|` 恰好在故障最严重处塌掉。这正是 `BaselineTracker` 保留长基线的理由（SMD 上短基线 0.562、长基线 0.772），所以这个测试断言的是 `0.5 < 上限 < 1.0`——**一个在这条数据上算出 1.0 的读者，才是测量写错了**。

还有一条同类的过程教训：这个脚本第一版报告"0 个 ratio、100% 窗口无对比"，原因是正则用了 `^` 却没加 `re.MULTILINE`。一个读起来像域属性的测量假象，必须在写进文档之前先被怀疑。

**结论**：SMD 上的瓶颈是引擎，不是状态，也不是成本策略。`catalogue()` 里的八个候选没有一个能动这一列——它们改的是喂给 System 2 的材料量和门限，而这一列量的是 Laya 对 digest 的判断力。这也让 3.10 节那次 `host-features-3` 的质量接受的解释更明确：它之所以有效，是因为特征变少让引擎答得更有区分度，而不是因为循环学会了检测。



### 3.12 让循环评判一次架构改动：`digest.scoring`，以及"接受指标 ≠ 上线指标"的陷阱

第 6 节第 2 条一直是一句话而不是一次测量：状态上限说明引擎在宿主域上不比一条阈值强，但候选目录只能改设置项，不能改打分路径。这一轮把它做成了能被执行、能被标签列评判的对象。

**开关。** 新增 `src/brian/scoring.py`，`SCORING_MODES = ("model", "deviation")`，`judge(digest, settings, laya)` 是唯一决定"谁来回答"的地方——`Pipeline.process_digest` 与 `Evaluator.run` 都调它，所以评测与生产走的是同一条路径。`DigestSettings.scoring` 默认 `"model"`，环境变量 `BRIAN_DIGEST_SCORING`，拼错的值抛 `ValueError` 而不是静默退回模型（静默退回会让测出来的结论描述的是另一个系统）。

**读者。** `deviation_score()` 取渲染器实际会印出的那些行里最大的 `|ratio − 1|`（优先长基线，因为宿主渲染器优先它，一个峰值来自文本里不存在的数字的读者是在测另一个系统）；`deviation_probability = dev / (1 + dev)`，严格单调，所以 AUC 与 at-budget 指标与直接在偏离度上算完全一致，同时把 dev = 0.5 映射到 0.33——SMD 流门限所在的位置。`deviation_result()` 返回一个形状与引擎相同的 `LayaResult`，`answers` 里**只有** `is_anomaly`，`raw.answers_absent` 明确列出 `category`/`severity`/`needs_human`/`blast_radius`：没有问过的题不许编答案。一条记录的头几个窗口还没有基线，读者无话可说，`judge` 此时回落到模型——"没有偏离度可测"与"没有故障"是两个不同的发现。

**两次测量，差在一个"切点由谁选"。**

| 配置 | shipped 门限（0.33）recall / fpr | 10% 误报预算下 recall / fpr |
|---|---|---|
| 引擎（8 特征） | 1.000 / 1.000 | 0.060 / 0.046 |
| 偏离度读者 | 0.993 / **0.935** | **0.142** / 0.098 |

第一列是"照现在的配置线上会发生什么"：引擎在 0.33 上对**每一个**窗口告警（recall 1.000 是因为 fpr 也是 1.000），读者在同一个 0.33 上告警 93.5%。第二列是循环用来比较的 at-budget 曲线：每台主机自己选切点后，读者捞到 0.142，引擎 0.060。

**这一轮最重要的发现不是读者更好，而是两条列的差距被第一次看见。** 循环第一次跑（记录 `var/evolve/round-20261117T200320Z.json`）把 `digest.scoring=deviation` **单独**接受了：recall@b 0.060 → 0.142，tokens/report 3349 → 3350。同一张表的下半部分同时印着 `shipped threshold host-scoring-deviation recall 0.993 fpr 0.935`——**一个被接受、但按记录下来的那一项配置上线会在 93.5% 的窗口上告警的改动**。披露行被打印出来了，但没有任何规则读它：at-budget 指标重扫的是循环自己选的切点，它结构上看不见"记录下来的补丁在线上门限处是什么行为"。

**修法是把候选从"一个字段"改成"一个补丁"。** `Candidate` 新增 `also: Tuple[(section, field, value), ...]`、`changes()`、`apply()` 按 section 分组后走 `apply_overrides`（所以伴随改动与第一项接受同一套校验，不会半应用），版本文件导出每一个变量、回滚一行一个 `unset`。理由写在 docstring 里：一项改动可能只有在两项同时生效时才可用，单字段目录只能表达这类修复的一半——于是"循环拒绝了它"就变成了关于**目录**的陈述，而不是关于改动的陈述。这也是调查里允许有界循环做的那一类自我改动：目标函数与验证方式都不动，只改提议的形状。

**补丁版的候选** `host-scoring-deviation = {digest.scoring=deviation, gate.anomaly_probability=0.87, gate.notify_probability=0.87}`（记录 `var/evolve/round-20261117T200724Z.json`，522 秒）：

| 候选 | recall@b | fpr@b | tok/rep | tok/win | shipped recall / fpr | 判决 |
|---|---|---|---|---|---|---|
| baseline | 0.060 | 0.046 | 3349 | 3327.5 | 1.000 / 1.000 | — |
| `host-scoring-deviation` | **0.142** | 0.094 | 3352 | **364.2** | 0.142 / 0.098 | **接受（质量 + 成本）** |

tokens/report 几乎不动（每次告警的 prompt 一样长），而 tokens/window 掉了 **89%**：告警从 828 个降到 **103 个**（`v002.json` 的 `reports`；`tp 19 / fp 68 / fn 115`，precision 0.218）。这一次两列是自洽的——错报率从 100% 降到 9.8%，在同样的 10% 预算下还多捞到 2.3 倍的故障。

版本文件 `var/evolve/versions/v002.json` 记下三行环境变量和三条回滚：

```
export BRIAN_DIGEST_SCORING=deviation; BRIAN_GATE_ANOMALY_P=0.87; BRIAN_GATE_NOTIFY_P=0.87
rollback: unset BRIAN_DIGEST_SCORING  unset BRIAN_GATE_ANOMALY_P  unset BRIAN_GATE_NOTIFY_P
```

四点必须说清。

**一、0.87 是在同一批 948 个窗口上选的**（把 10% 预算规则用在读者自己的分数上），所以这一轮验证的是自洽性，不是泛化。这个常数可以从 `scripts/real_data/deviation_reader.py` 复现（不需要 Laya）：cut 0.70 → 0.731/0.317，0.80 → 0.440/0.202，0.85 → 0.187/0.120，**0.87 → 0.142/0.098**，0.90 → 0.067/0.056，脚本直接打印"10% 预算内最低切点：p ≥ 0.87"。循环的接受判定仍然用每台主机自己扫出来的切点，这个常数只影响"shipped 那一列"和"有没有告警可计费"，不参与判定——这是它不能被写成"循环自己发现了 0.87"的原因。

**二、读者不是更好的检测器，只是更便宜的排序。** 10% 误报预算下它只找到 14% 的故障，引擎 6%，两者都很差；差别在于读者跑一次 0 ms、0 token，而且它的 shipped 配置终于是可用的。2-2 上读者的 AUC 只有 0.597（3.11 节），这个域的检测问题并没有被解决。

**三、补丁式候选让每个候选可以藏一个坏的一半。** 成本列与证据列分别看的是"这份补丁整体"，一项降低成本的伴随改动可以掩护另一项抬高误报的改动。这就是版本文件必须逐行导出变量、逐行给出回滚、并在表里同时印 shipped 列的原因——读到"接受"的人必须能自己判断是哪一半在起作用。

**四、拒绝账本改成"最新判决 + 它来自哪条流"。** 原来的实现保留每个候选的**第一次**拒绝，于是 `events-8` 在跑 SMD 时被标成"已被拒绝"，而它在 SMD 上其实被接受过。判决是关于数据集的，保留第一次等于把后来被推翻的结论当作定论——那是带着手续的失忆。现在 `previously_rejected()` 输出形如 `on SMD: keeps less evidence: ...`，并有一条测试专门钉住"另一条流上的接受会清掉拒绝"。



### 3.13 把"上线会怎样"写成规则，并让历史判决可以重审

3.12 节留下的缺口是一句话："`decide()` 只读 at-budget 曲线，所以它接受了按记录配置上线会告警 93.5% 窗口的改动。" 这一轮把它变成两条可执行的东西。

**一、部署地板（`EvolveRules.deploy_fpr_ceiling = 0.50`）。** 它在**任何比较之前**检查：一份候选按记录下来的配置在线上会告警的窗口比例超过这个上限，就判 `deploy` 拒绝，理由是 `would alert on 94% of windows at its shipped threshold (the deployed configuration: 100%), above the 50% deploy ceiling`。

**这条规则的第一版是错的，而错法本身值得记下来。** 第一版无条件拒绝"shipped FPR 超线"的候选，于是在 SMD 上它把 `events-8`（−27%）、`events-4`（−36%）、`reuse-300`、`page-severity-3`、`confidence-auto-85` 全判了 `deploy`——因为这些候选**根本没动打分路径**，它们的 shipped FPR 是 1.000，和基线一模一样。SMD 上 shipped 0.33 会对每个窗口告警，这是**部署的既有缺陷**；把"没有摸到这条路径的候选"也按这条线判死，等于在一个部署已经坏掉的域上拒掉所有纯成本改动，而那恰好是唯一不会让它更坏的改动。

修法是把规则的对象说清楚：**候选要为自己新声称的可部署性负责，不为它继承下来的缺陷负责。** 所以拒绝的条件是"shipped 行为**变了**且仍在线外"；如果 shipped recall 与 FPR 与基线逐位相同，那就是继承——按警告印出来，判定回到成本/质量两列。第一版跑出来的表（`var/smd_round6.txt`）因此被保留下来当作这条规则的取证：它说明一条"看起来很严"的规则可以在不违反任何测试的情况下，把五条正确的接受全部推翻。

**二、重审历史（`brian evolve --rejudge <round-*.json>`）。** 它把已记录的测量值重新过一遍今天的 `decide()`，不重跑、不调 Laya、不花 token，只把判决变化列出来：

```
round             var\evolve\round-20261117T200320Z.json
recorded rules    {"cost_tolerance": 0.05, "fpr_budget": 0.1, ...}
current rules     {"cost_tolerance": 0.05, "deploy_fpr_ceiling": 0.5, "fpr_budget": 0.1, ...}

candidate              recorded               re-decided             change
--------------------------------------------------------------------------------
host-scoring-deviation accept quality         reject deploy          -> reject

host-scoring-deviation was accepted: recall at 10% false positives 0.060 -> 0.142 for 3349 -> 3350 tokens/report
                       now  rejected: would alert on 94% of windows at its shipped threshold, above the 50% deploy ceiling

1 verdict(s) changed. The stored measurements were not re-run; only the rules moved.
```

对 3.12 节的补丁版（`round-20261117T200724Z.json`）重审的结果是 `no verdict changed`——规则变了，正确的那个判决没变。这正是重审的用处：**它同时证明新规则能抓住旧错误，且没有把对的判决一起推翻。** 这也是调查里第二个开放问题（可观测、可版本化的验证基础设施）最小的一次落地：没有它，改一次规则就等于悄悄改写历史，没人分得清"改进"和"记账口径变了"。

`Candidate.from_dict`/`Measurement.from_dict` 是这件事的前提，并且**同时接受两种记录形状**——补丁前的单字段记录（`section`/`field`/`value`）和补丁后的 `changes` 列表。最值得重审的恰恰是旧记录：那是在旧规则下做出的决定。

规则修好之后重跑的完整一轮（`round-20261117T203743Z.json`，9 个候选 → 4 接受 5 拒绝，版本文件 `v003`–`v006`）：

```
candidate              recall@b   fpr@b   tok/rep   tok/win evidence verdict
baseline                  0.060   0.046      3349    3327.5     100% -
events-8                  0.060   0.046      2438    2422.7     100% accept
events-4                  0.060   0.046      2135    2121.2     100% accept
reuse-300                 0.060   0.046      3349    3327.5     100% reject
analyze-pages-only        0.060   0.046      3349    3327.5     100% reject
page-severity-3           0.060   0.046      3357    3336.1     100% reject
confidence-auto-85        0.060   0.046      3349    3327.5     100% reject
anomaly-p-40              0.060   0.046      3323      73.6     100% reject
host-features-3           0.187   0.089      2887    1108.6     100% accept
host-scoring-deviation    0.142   0.094      3352     364.2     100% accept
```

和第一版规则跑出来的那张表（`var/smd_round6.txt`）逐行对比，唯一的变化就是那五个 `deploy` 拒绝全部退回成本判定——`events-8`/`events-4` 重新被接受，另外三个按原有的"5% 门槛"理由拒绝。**这就是"继承的缺陷不算候选的账"在数据上的样子。** 表尾同时印着 shipped 列与那条 `! the deployed configuration already alerts on 100% of windows` 的警告：循环没有把 1.000 当成自己的成绩，也没有拿它去否决别人。

`previously_rejected(root, rules)` 现在把这件事自动化了：`cmd_evolve` 传入**今天的**规则，账本在打印"这些已被否决"之前会先把每条存下来的测量重判一遍，理由前缀 `re-judged: `。没有这一层，一次规则改动之后，下一轮会继续用退役规则下的结论挡住正确的候选——**一个用旧口径继续毒害新回合的账本，比没有账本更糟**。

三点必须说清。

**一、0.50 是一个判断，不是一次测量。** 没有任何数据说"误报超过一半就不可部署"是正确阈值；它只是一条明显不该被越过的线，而 3.12 节那个配置（0.935）离它很远。要把它变成测量，需要每一页告警的人工成本，而这个仓库没有那个数。

**二、重审只重放数字，不重跑。** 所以它能回答"今天的规则会怎么判这份记录"，不能回答"今天的配置今天测出来是什么"。后者要重跑（一次 SMD 全目录约 9 分钟）。

**三、部署地板对"只动阈值"的候选仍然不够。** 边界 9 说的盲区还在：at-budget 曲线看不见阈值移动，`deploy` 只是事后否决，不是"让它可判"。要让阈值类候选真正进入比较，需要一个直接读告警量的列。

### 3.14 给"只动阈值"的候选一列可判的账：`operational`

**问题出在测量的构造上，不是规则的松紧。** at-budget 曲线是"把这次跑出来的概率重扫一遍、取预算内最好的切点"，所以任何只改 `anomaly_probability` 的候选在这条曲线上**恒等**——它动的正是曲线要忽略的那个常数。3.13 节的部署地板只是事后否决，门限内的阈值候选仍然被这条盲曲线判成"没有变化"。这就是边界 9/11 记下的那个洞。

**补法：用"记录下来的配置本身"做第三列。** `deployable()` 两个条件，缺一不可：

1. `fpr <= deploy_fpr_ceiling`——没人会去接 90% 窗口的告警；
2. `recall >= recall_at_budget`——按记录下来的配置跑出来的召回，不低于循环自己扫出来的那个切点的召回。

第二条是这一节的支点：它说**一个可部署的配置不能被循环自己算得出来的切点打败**。这不是在夸某个阈值好，只是在排除"明显比免费替代更差"的配置。于是判定多了一条接受路径 `operational`：基线不可部署、候选可部署，且账单涨幅不超过 `cost_tolerance` → 接受。

写法上它排在证据检查之后、成本比较之前，因为只有它能让 at-budget 恒等的候选第一次动起来；同时它**不豁免账单**——把可部署性修好但价格翻三倍不是 Pareto 移动，测试 `test_a_deployability_fix_does_not_license_an_unbounded_bill` 钉住这一点。

SMD 上的四个配置正好把这三个条件都走了一遍（`alerts/1k` = 1000 × shipped FPR，就是"每千个窗口会响多少页"）。这张表摘自第 7 轮的实测（`var/smd_round7.txt`，记录 `var/evolve/round-20261117T210036Z.json`）：

| 记录下来的配置 | recall | alerts/1k | at-budget recall | `deployable` |
|---|---|---|---|---|
| shipped 0.33（8 特征，基线） | 1.000 | 1000 | 0.060 | 否——超过 50% 地板 |
| `anomaly-p-40`（8 特征） | 0.007 | 27 | 0.060 | 否——比免费切点还差 |
| `host-features-3`（0.33） | 0.291 | 399 | 0.187 | **是** |
| `host-scoring-deviation`（0.87） | 0.142 | 98 | 0.142 | **是** |

重审第 6 轮的记录时，这张表把两个 `accept` 的**理由**改了名：`host-features-3` `accept cost` → `accept operational`，`host-scoring-deviation` `accept quality` → `accept operational`。判决没变，规则变了——`--rejudge` 现在专门印一行 `still accepted, but the rule that decides it moved quality -> operational`（拒绝的情形印 `still rejected, but the rule that decides it moved gain -> operational`），而不是把理由变化混进"没有变化"。这正是重审该做的事：**判决稳定、理由可追溯**。

第 7 轮在 SMD 上的完整结果是 4 接受 / 5 拒绝，与第 6 轮的判决一致，但接受的理由换了口径：`events-8`（3349 → 2438，−27.2%）与 `events-4`（3349 → 2135，−36.3%）仍以 `cost` 接受；`host-features-3` 与 `host-scoring-deviation` 以 `operational` 接受（理由里直接写"从每千窗 1000 次告警、召回 1.000，变成 399 次、召回 0.291"这种句子）；`anomaly-p-40` 被拒绝——**而它现在的拒绝理由终于是对的**：不是"曲线看不见它"，而是"按记录下来的配置会漏掉比免费切点更多的故障"（见下）。

三点必须说清。

**一、"不低于免费切点"是定义，不是测量。** 它不产生任何关于"多少误报可以接受"的信息，那个数仍然是 3.13 节的判断（0.50）。它只排除一类配置：被一个循环自己就能算出来的切点严格支配的配置。要让这条线变成测量，还是需要每页告警的人工成本。

**二、这一列目前只在两个部署修补上亮过绿灯，没有一个纯阈值候选通过。** 在 8 特征上，任何门限要么对全部窗口告警（1000/1k），要么静音到召回 0.007——因为引擎在那个特征集上的排序接近机会线（3.7 节 AUC 0.511）。**这不是规则太严，这是引擎排不出序**：状态上限 0.827 说明文本可分，而引擎读出 0.674/0.347。要在这条流上让纯阈值候选通过，先得让引擎的排序变好，阈值本身不是瓶颈。

**三、`operational` 与部署地板是同一件事的两面，不是两条独立证据。** 地板否决"变更后仍不可部署"，`operational` 接受"从不可部署变成可部署"；两者都只读 shipped 那一列，所以都不能替代 at-budget 关于排序的判断。

**四、第三条出口需要配两条"不许靠沉默"的规则。** 有了 `operational` 之后，还有两个洞：

其一，反向的部署回退没有被命名。基线在"比免费切点不差"这一条上是通过的（shipped 0.33 虽然对每个窗口告警，但至少没有漏掉故障），而 `anomaly-p-40` 把它换成了"每千窗 27 次告警、召回 0.007"——**比循环自己算得出来的切点还差**。于是 `decide()` 加了一条对称的拒绝（规则名也叫 `operational`）：`beats_own_cut(baseline) and not beats_own_cut(trial)` → 拒绝，理由是 `the recorded configuration would page on fewer failures than the cut the budget rule already computes: shipped recall 0.007 against 0.060 at the 10% budget`。同一列既接受也拒绝，阈值类候选第一次被它要动的那个东西判定。

其二，成本路径本身留着一个更容易中招的洞：一个候选只要显著降低账单、同时在 shipped 门限上少报一些故障，at-budget 曲线又不动（把窗口切小、把告警条件收紧都行），就能以 `cost` 被接受——**它省下的钱是用漏报换的**。所以再加一条 `silence` 拒绝：只要"账单降幅超过 `min_gain`"且"shipped recall 下降"且"at-budget recall 没有达到 `min_quality_gain` 的涨幅"，就拒绝，理由写成 `pays for a 40% smaller bill with silence: shipped recall 1.000 -> 0.007 while recall at the 10% false-positive budget did not rise`。

两条规则都只在"真省钱 + 真少报 + 预算列没涨"或"预算列本来就通过、被候选弄失败"时触发，所以不会误伤 3.13/3.14 里那两个合规的部署修补（它们的账单只涨不降），也不会误伤纯成本候选（它们 shipped 列不变）。当前 catalogue 上 `silence` 一次都没有触发——这本身是个好消息：**这一轮没有候选尝试用沉默换账单**；但规则必须在，因为下一轮加候选时诱惑就在那里。

---

### 3.15 机器 2-2 低于机会线：不是引擎看不见，是 digest 里根本没有电平

上一轮把"解释 2-2 为什么低于机会线"列为下一步第一项，这一轮先做诊断（`scripts/real_data/host_diagnosis.py`：不调 Laya，只重放生产路径 `Windower → build_digest(profile="host") → BaselineTracker.observe`，把每个渲染出来的指标在 `value`、`delta_ratio`、`long_delta_ratio` 三个字段上分别算 AUC）。

**一、同一个窗口，`value` 有信号，比值没有。**

| 指标 | 1-6 `value` | 2-2 `value` | 1-6 `ratio` | 2-2 `ratio` |
|---|---|---|---|---|
| CPU | 0.897 | 0.690 | 0.348–0.553 | 0.472–0.536 |
| CPU system | 0.903 | 0.650 | ″ | ″ |
| load 1m | **0.912** | **0.753** | ″ | ″ |
| free memory | 0.877 | 0.489 | ″ | ″ |
| swap used | 0.805 | 0.510 | ″ | ″ |
| disk write | 0.814 | 0.596 | ″ | ″ |
| net in | 0.598 | 0.708 | ″ | ″ |
| net out | 0.652 | **0.780** | ″ | ″ |

两台主机的比值列都在 0.35–0.55——**比值把信号基本扔掉了**。原因不是模型，是分母：2-2 有 **40%（11812/29649）** 的比值建在近零基线上（`|baseline| < 0.02`），那是算术噪声穿上了"1.07x"的外衣；1-6 只有 1.6%。

**二、digest 里连电平都没有。** `_render_host_digest` 用 `_num(entry["value"], 2)`（`"%.2f"`）打印电平，而 2-2 的 CPU 在 **0.0016** 附近，所以每个窗口都渲染成：

```
Host smd-machine-2-2, a 300-second window with 5 samples.
CPU 0.00 (baseline 0.00, 1.07x).
load 1m 0.00 (baseline 0.00, 0.86x).
```

**电平信息一个字都没有进文本，只有取整噪声的比值。** 引擎不是"把信号读反了"，它读到的是一段空文本——3.11 量到的"简单读者 0.827 > 引擎 0.674"因此不是引擎的失败，而是"读者用的是结构化字段，引擎用的是被磨平的文本"。

**三、修的是渲染缺陷，判的是因果。** 新增 `_host_num(value, precision)`：绝对值 ≥ 1000 仍打整数，**0.01 以下按有效位加宽小数**（0.0016 → `0.0016`，0.00051 → `0.00051`），0.01 以上保持原来的两位——因为 0.01 起两位小数已经给出两位有效数字，而 3.11 的固定渲染是量过的产物（`0.08`、`0.25`），不能为了格式偏好挪动。`DigestSettings.host_precision`（默认 2，env `BRIAN_DIGEST_HOST_PRECISION`）只控制精度，加宽是修缺陷、不是可选项。

修完之后同一段文本变成 `CPU 0.020 (baseline 0.00)`、`net in 0.11 (baseline 0.013, 8.22x)`——信息回来了。

**四、然后动手测，结论是否定的。** 通过生产路径（`Evaluator → build_digest → judge → LayaClient`）在同一批窗口上重测：

| 主机 | 精度 2 | 精度 3 | 同文本简单读者 |
|---|---|---|---|
| machine-1-6（414 窗，78 正例） | AUC 0.523，recall@10% 0.077 | 0.538，0.115 | **0.827** |
| machine-2-2（414 窗，78 正例） | AUC 0.297，recall@10% 0.018 | 0.436，0.000 | **0.597** |

也就是说：电平回到文本里之后，2-2 的 AUC 从 0.297 起到 0.436（同一批窗口、只换文本，所以差异不是样本噪声），1-6 几乎不动。**但两台都仍然远低于同一段文本的 0.827 / 0.597。** 那个"0.00"是真缺陷，却不是瓶颈——它把"引擎读不懂主机状态"这件事伪装成了"文本是空的"。修掉伪装之后，结论反而更硬：**引擎在这种状态上的排序能力本身不足**。

**五、循环对"多一位精度"说了不。** 把它做成候选 `host-precision-3` 交给循环（`--candidates host-precision-3 --apply`）：

```
candidate              recall@b   fpr@b   tok/rep   tok/win evidence verdict
baseline                  0.060   0.072      3350    3328.6     100% -
host-precision-3          0.060   0.078      3363    3342.2     100% reject

x host-precision-3       spends 0.078 of the 10% false-positive budget against 0.072 without
                         finding more incidents: recall 0.060 -> 0.060
```

在池化到 10% 预算之后，"多一位数字"没有换来任何召回，只多花了一点预算和一点 token。这和主机的逐台 AUC 差异（0.297→0.436）不矛盾：**逐台 AUC 说排序略好，池化到统一预算说不值**——两台主机不共享一把尺，这正是 3.7 那条"per-recording cut"的同一个教训，只是这次它出现在候选被拒这一侧。

**六、这一轮真正得到了什么。** 不是"2-2 修好了"，而是三件事：(1) 一个可复现的渲染缺陷被修掉，并且被 pinned 测试挡住（电平小于 0.01 不得打印成 `0.00`；服务档渲染不受影响）；(2) "文本为空"这个混杂因素被排除，于是 3.11 的结论从"可能是渲染问题"升级为"引擎排序能力不足"；(3) 一条候选被真实数据拒绝——**包括我自己刚写的修复的加强版**。

### 3.16 把噪声比值从 digest 里拿掉：剂量反应是负的，以及一次被自己规则挡住的判决

3.15 节量到两件事：2-2 有 **40%** 的比值建在 `|baseline| < 0.02` 的基线上（`4.17x` 描述的是两个都近似为零的数），而比值列在 2-2 上的可分性只有 0.47–0.54，电平列有 0.69–0.78。于是自然的假设是：**引擎被这些噪声比值带偏了，把它们拿掉它就能读到电平。**

**一、实现。** 新增 `DigestSettings.host_ratio_floor`（env `BRIAN_DIGEST_HOST_RATIO_FLOOR`，默认 0.0 = 现在的渲染）：渲染时若 `|baseline| < floor`，**省掉比值、保留电平与基线**——不是省掉整行，因为那会把"测到了什么"也一起扔掉。默认 0 让 3.11 的 pinned 渲染一字不动，测试同时钉住"0.02 下噪声比值消失、真基线比值（1.15x）保留"。

**二、剂量反应，两次测量。** 做成两个候选一起交给循环（`--candidates host-ratio-floor-02,host-ratio-floor-10 --apply`，记录 `var/evolve/round-20261117T214547Z.json`）：

```
candidate              recall@b   fpr@b   tok/rep   tok/win evidence verdict
baseline                  0.060   0.072      3350    3328.6     100% -
host-ratio-floor-02       0.060   0.052      3347    3326.0     100% reject
host-ratio-floor-10       0.045   0.049      3344    3319.5     100% reject
```

- `0.02`：召回不变，花的预算从 7.2% 降到 5.2%，但账单只降 0.1% —— 按成本条（5%）被拒，**这是一个诚实的"没坏也没用"**。
- `0.10`：召回 0.060 → **0.045（−25%）**。也就是说把大部分比值拿掉之后，引擎在预算内能捞到的故障更少了：**噪声比值不是干扰项，它们确实是那台主机上几乎全部的排序依据**（哪怕那个依据很烂）。

剂量反应是单调负向的，所以"拿掉噪声比值能让引擎读到电平"这个假设被否定。结合 3.15，结论变成：**引擎在主机状态上的失败不是文本质量问题**——把电平打印出来没用（3.15），把噪声比值删掉也没用（这里）。

**三、这一轮真正的意外：一条判决被我自己的规则挡住了。** 表格里 `host-ratio-floor-10` 的拒绝理由写的是部署地板：`would alert on 100% of windows at its shipped threshold (the deployed configuration: 100%), above the 50% deploy ceiling`。但它的 shipped 告警是 **999/1000**，基线是 1000/1000——地板把"0.999 与 1.000 不同"当成了"候选在宣称一个新的部署状态"，于是用一条形式理由盖住了真正的理由（标签列已经因为它丢了 25% 的召回而拒绝它）。

3.13 节那条"继承的缺陷不该记在候选账上"的规则用的是**精确相等**，太严。改成 `EvolveRules.deploy_fpr_tolerance: float = 0.005`（千分之五的窗口以内算继承），并在 docstring 里写明这是一个判断而不是测量。改完之后对**已记录的**这一轮重审：

```
candidate              recorded               re-decided             change
host-ratio-floor-02    reject gain            reject gain            -
host-ratio-floor-10    reject deploy          reject recall          same verdict, new rule

no verdict changed: today's rules agree with the recorded round.
host-ratio-floor-10    still rejected, but the rule that decides it moved deploy -> recall
```

判决没变，理由从 `deploy` 变成 `recall`——**历史判决的可审性在这里第一次派上实际用场**：修规则不需要重跑 9 分钟的评测，`--rejudge` 把账本里的旧判决按新规则重放，并明确写出"只有规则动了"。

**四、还有一个不能在文档里含糊的边界：3.11 的上限是"字段"的上限，不是"文本"的上限。** 状态上限用的是 `MetricValue` 里的 `delta_ratio`/`long_delta_ratio`，而读者（`digest.scoring=deviation`）也是读字段而不是读渲染出来的句子——所以 `ratio_floor` 对读者**按构造无效**（实测：同一窗口三种 floor 下读者概率都是 0.5167）。这意味着 0.827 / 0.597 说的是"这份结构化状态里能榨出多少"，而不是"这段文本里能榨出多少"；引擎只拿到文本，所以它和上限之间有一层信息差，而这一层既不是引擎的错，也不是上限在说谎。

### 3.17 上限是"文本"的还是"字段"的：两个读者，同一个答案

3.16 节末尾留下了一个含糊：`state_ceiling` 读的是结构化字段，引擎读的是渲染出来的一句话。0.827 / 0.597 到底是"这份状态里存在多少信息"还是"这段文本里存在多少信息"？这一节把它量清楚了。

**一、做法：再加一个只读文本的读者。** `src/brian/scoring.py` 新增 `text_deviation(text)`：从渲染文本里把所有 `Nx` 比值解析出来，取 `max |x − 1|`。它和字段读者的唯一区别是**只能看见引擎能看见的东西**——被 `host_ratio_floor` 省掉的比值它看不到，被舍入的电平它也看不到（它只看比值）。`state_ceiling` 现在同时返回两列：`by_stream`（字段）与 `text_by_stream`（文本），`brian evolve` 每轮把两者并排打印出来。

**二、测量（948 个窗口，两次解析，无 Laya 调用，`var/measure_ceilings.py`）：**

```
config          recording           fields    text    engine
all 8 features  smd-machine-1-6      0.827   0.827     0.517
all 8 features  smd-machine-2-2      0.597   0.598     0.299
3 features      smd-machine-1-6      0.886   0.886     0.674
3 features      smd-machine-2-2      0.516   0.521     0.347
```

前两列几乎完全相等。也就是说：**渲染没有丢掉字段读者用到的任何信息**，0.827 / 0.597 确实是**文本上限**，不是只存在于内存里的数字。3.16 节那条边界因此可以关掉——而且是以对引擎不利的方向关掉：文本里有 0.827 的可分性，引擎只交出 0.517（第三列的 8 特征数字是本轮新测的，`var/engine_auc.py`，两条记录各 414 个窗口，`anomaly_probability=0.33`；3 特征的 0.674 / 0.347 来自 3.7 节）。差距不是几个百分点，而是"从可用到不可用"。

顺带一个对 3.15 的补充：上一轮精度修复让 2-2 从 0.297 升到 0.436（那是 3 特征），但在这条流的 **8 特征**配置下它仍然是 0.299，比机会线还低。

**三、顺带复现了 3.7 的结论，这次是在上限一侧。** 只给 3 个特征时 1-6 的上限从 0.827 升到 **0.886**——特征越多，上限越低。这和 3.7 节"8 特征把生产渲染器稀释了"是同一件事的两次独立测量：一次是引擎 AUC（0.511 对 0.674），一次是上限本身。

**四、这个结果不证明的东西。** 两列相等只说明两个读者在**同一批窗口上排序几乎一致**（0.597 与 0.598 的差别是一个窗口的解析歧义），不说明它们给出同一个分数、也不说明 0.827 可以在生产上门限化。它证明的只有一件很窄但很关键的事：上限不是测量方式的产物。

**五、下一步因此变得唯一。** 3.15 / 3.16 否定了"文本质量"，这一节否定了"上限说大了"，于是剩下的可能性只有引擎本身：同一段文本、同一批窗口，它在故障窗口与正常窗口上的答案分布到底长什么样，以及它是否只在服务名这类与标签无关的 token 上分配注意力。

### 3.18 引擎到底在读什么：把信息拿掉，答案反而变高

前四节把能推给别处的理由都排掉了：文本质量（3.15）、噪声比值（3.16）、上限测量方式（3.17）。剩下唯一没被检验的是引擎本身。这一节直接问它：**同一段文本里换掉一个成分，答案会不会跟着动？** 如果答案不跟着动，那么无论怎么改配置、怎么改特征、怎么改门限，都不会有第二台主机上的检测。

**一、两个测量（`scripts/real_data/engine_attention.py`，`use_cache=False`，两条记录各 414 个窗口）。**

第一个是相关：把每窗答案和"最响的打印比值"、"文本长度"做相关。第二条记录的"上限 AUC"是 0.809 / 0.604，也就是同一段文本里确实有可分性——那么答案至少应该和它正相关。

```
recording        auc_engine  auc_text_deviation  p~deviation  p~length
smd-machine-1-6     0.517          0.809            -0.110      -0.054
smd-machine-2-2     0.299          0.604            -0.161      +0.248
```

答案是**微弱负相关**：文本里偏离越大，引擎给的异常概率略微**更低**。2-2 上它和文本长度正相关（0.248），也就是说它更多是在反应"这段文本有多长"，而不是"这些数偏离多远"。

第二个是受控替换，同一窗文本四种写法（均衡取 20 个故障窗 + 20 个正常窗，取比值最响的）：

```
recording        class    n    verbatim  renamed  flattened  amplified
smd-machine-1-6  fault   20     0.2360    0.2356     0.2487     0.2331
smd-machine-1-6  normal  20     0.2320    0.2337     0.2405     0.2299
smd-machine-2-2  fault   20     0.2271    0.2256     0.2570     0.2251
smd-machine-2-2  normal  20     0.2058    0.2053     0.2319     0.2179
```

三种替换，结论一个比一个糟：

- **改名无效**：把 `smd-machine-1-6` 换成 `smd-machine-2-2`，答案变化 0.0004–0.0017。它不是在认主机名。
- **放大幅度无效**：所有比值放大 1.5 倍，答案反而略降（0.2360 → 0.2331）。信息变得更明显，判断没跟着更新。
- **抹平比值反而升**：把所有比值改成 `1.00x`（**把信号彻底删掉**），答案从 0.2360 升到 0.2487 —— 比任何真实故障窗都高。

最后一条是决定性的：**把状态里的信息全部拿掉，引擎会比读到真实故障时更倾向于说"异常"。** 这说明它在这类文本上给出的不是"检测"，而是一个近乎常数的偏置（0.21–0.26 之间小幅抖动），抖动方向与信号无关甚至相反。

**二、为什么这解释了两台主机的差距（0.517 对 0.299）。** 如果答案是围绕常数的噪声，那么 AUC 就只取决于噪声恰好更偏向哪一类：1-6 上偏向故障（0.517），2-2 上偏向正常（0.299）。这也解释了为什么"换 3 个特征"能同时把 1-6 抬到 0.674、把 2-2 压到 0.347——换个文本形状，噪声的偏向也换了一次，与"哪个特征更有信息"无关。

**三、这一节把循环的路线定死了。** `Imp` 的候选目录里所有条目（事件数、复用窗口、特征子集、评分方式、门限、精度、比值地板）都作用在"喂什么"和"判多严"上；没有一个作用于"这个编码器能不能读这种文本"。所以在它被换掉或换模型之前，**在这条流上继续调参数的期望收益是零**——这不是悲观判断，是 3.15–3.18 四次独立测量之后的排除法。

### 3.19 换形状、换措辞、换问法：五个渲染 × 两个问句，没有一格稳定

3.18 只否证了一种文本形状（`Host X, a 300-second window with 5 samples. | CPU 0.19 (baseline 0.17, 1.07x).`）。这一节把"形状"这件事本身做成一个 2 × 5 的格子：同一个窗口渲染五次，每份都问两次。脚本是 `scripts/real_data/host_readability.py`（`use_cache=False`，均衡取 100 个故障窗 + 100 个正常窗，按打印出的最大偏离挑选，即"最强的那些窗"）。

五种渲染：

```
full     CPU 0.11 (baseline 0.20, 0.56x).        shipped digest
values   CPU 0.11.                               只有电平，去掉比较
ratios   CPU 0.56x.                              只有比较，去掉电平
words    Cpu is 44% below its normal level.      把比较说成一句话
header   只有第一行（"Host …, a 300-second window with 5 samples."），一个数字都没有
```

两个问句：`catalog` 是流水线真正发出去的那份（`brian.laya.qcatalog.DEFAULT_QUESTIONS`），`bench` 是 `scripts/real_data/bench_smd.py` 里取得 0.745–0.758 那批归档跑用的措辞。`header` 是控制组：它的文本对所有窗口**完全相同**。

```
recording        question  full    values  ratios  words   header
smd-machine-1-6  catalog   0.6062  0.6705  0.1716  0.6611  0.5000
smd-machine-1-6  bench     0.4403  0.2150  0.3312  0.6646  0.5000
smd-machine-2-2  catalog   0.4704  0.4948  0.5110  0.3730  0.5000
smd-machine-2-2  bench     0.5778  0.5627  0.4257  0.3538  0.5000
```

四点读法：

- **控制组是干净的。** `header` 在四条臂里都只产生**一个**不同答案（`distinct=1`），AUC 恰好 0.5000，故障均值与正常均值逐位相等。所以引擎对同一输入是确定的，所有变化都来自文本内容，不是采样噪声。
- **格子横跨 0.17–0.67，方向在两条记录之间翻转。** 1-6 上最好的是 `values`（只有电平，0.6705），2-2 上最好的是 `full`（0.5778）；`ratios` 在 1-6 的 catalog 臂是 0.1716（远低于机会线），在 2-2 的 catalog 臂是 0.5110。**没有一种形状在两条记录上都赢**，这与 3.18 的"偏置随形状移动"一致：换形状只是把常数搬到别处。
- **问句比内容更有力。** 同一份 `full` 文本，1-6 上 catalog 问句 0.6062、bench 问句 0.4403；本轮回合早期另有一份措辞给出 0.2874。**换一句话，比换三种数字排列的摆动都大。** 这解释了 3.18 里"抹平比值反而升"的怪现象：那个数本来就主要由提示词决定。
- **样本构成也能移这么多。** 3.18 在同一条流的自然分布上测到引擎 0.517 / 0.299；这一节在"最响偏离"的均衡样本上测到 0.6062 / 0.4704。n≈180 时 AUC 的标准误约 0.045，所以这两组数字的差异有相当一部分只是窗口怎么挑。凡引用本节数字都必须带上"均衡、按最响偏离挑选"这句话。

**这一节落地的那一条改动：`digest.scoring="auto"`。** 证据支持的结论不是"再改渲染"，而是"两类状态该由两个裁决者处理"：服务形态上模型能分（合成套件 0.875 召回 / 0.000 误报），主机形态上模型不能（本节与 3.18）。所以 `auto` 按 **digest profile** 解析——`host` 用确定性读者，`service` 用模型——而不是按某一批数据上更讨喜的那个数。两条验证：

- 服务路径不变：`BRIAN_DIGEST_SCORING=auto` 跑 `brian eval`，结果与模型默认逐位相同（召回 0.875、误报 0.000）。
- 主机路径不变：SMD 流的 profile 就是 `host`，所以 `auto` 在该流上等同于 `deviation`，3.12 节被接受的 `v002`/`v008` 补丁在这条流上的数字不变。

**这条改动不能宣称的事**：它没有让主机检测变好。读者在 10% 误报预算下是 0.142 召回，引擎 shipment 门限下是 0.007；两者的差距是"用不用得上"，不是"检测到了 SMD 故障"。而且本节无法分开两种解释——"这个编码器做不了这种比较"与"这个问句问的是模型没有先验的对象"——两者对运维的含义相同（这类状态不要交给它判），对模型设计的含义不同。

---

### 3.20 读者的切点不跨时间：0.87 是样本内的数字，能用的是排序而不是常数

边界 12 从 3.12 节起一直写着"读者自己的 0.87 是在同一批窗口上选的"。这一节把它量掉：把切点在一半窗口上拟合、在另一半上评估，再看跨主机、看记录内部的电平漂移（`scripts/real_data/deviation_transfer.py`，纯函数，不调 Laya）。

两种定切点的办法：**pooled** 是"在拟合集上取满足误报预算的最高召回切点"，它用了标签，所以是乐观上界；**quantile** 是"取分数最高的那 `budget` 份额"，不需要标签，是部署唯一能算的版本。

```
cut fitted by pooled (labels of the fitting set)
split                          fit  test  cut     recall(in) fpr(in) recall(out) fpr(out)
smd-machine-1-6 first->second  207  207   0.9091  0.444      0.091   0.000       0.000
smd-machine-1-6 second->first  207  207   0.5710  0.928      0.072   1.000       0.551
smd-machine-2-2 first->second  207  207   n/a     n/a        n/a     n/a         n/a
smd-machine-2-2 second->first  207  207   0.8413  0.232      0.099   n/a         0.159
smd-machine-1-6 -> smd-machine-2-2   414 414 0.8755 0.128      0.095   0.107       0.087
smd-machine-2-2 -> smd-machine-1-6   414 414 0.8653 0.161      0.092   0.141       0.110
```

**时间上完全不通。** 1-6 上"前半段拟合、后半段评估"得到 0 告警（0.000/0.000），反过来得到 100% 召回和 55.1% 误报——同一台主机、同一条流，切点错一个方向就关掉检测器，错另一个方向就淹掉它。改成 quantile（不需要标签）也一样：`first->second` 切 0.9091 → 0.000/0.000，`second->first` 切 0.8130 → 0.889/0.273。

**跨主机反而"看起来没事"**（0.107/0.087 与 0.141/0.110），这条最容易骗人：两条记录带着同一种不平稳，所以它们的平均对得上，而任何一条单独都不平稳。把跨主机迁移当成"泛化通过"正是本仓库反复犯的那类错。

原因在记录内部的**电平漂移**，用整条记录的 10% 切点按四段看：

```
recording  windows  median  p90     recall@cut  fpr@cut
1-6 q1     103      0.8462  0.9286  0.778       0.340
1-6 q2     104      0.5000  0.7363  n/a         0.000
1-6 q3     103      0.4344  0.5714  0.000       0.000
1-6 q4     104      0.7681  0.8340  0.046       0.000
2-2 q1     103      0.6250  0.8898  n/a         0.165
2-2 q2     104      0.6192  0.8422  n/a         0.058
2-2 q3     103      0.6486  0.8768  0.200       0.103
2-2 q4     104      0.6022  0.8333  0.095       0.036
```

1-6 的**中位数**在同一台主机的一条记录里从 0.846 掉到 0.434 再回到 0.768，p90 从 0.571 到 0.929；而故障与正常的**均值差只有约 0.03**。电平摆动比要分辨的差异大一个数量级，所以固定常数在这里不是门限，是对漂移的一次下注。2-2 相反：中位数稳在 0.60–0.65（平稳）但排序弱（AUC 0.603 对 1-6 的 0.809）。两台主机的失败方式不一样——1-6 是"能排但会漂"，2-2 是"不漂但排不准"。

**那么能上线的是什么？** 一条只依赖排序、因果、无标签的规则：*分数在最近 K 个窗口里进前 10% 就告警*。

```
rule                         scored  alerts  recall  fpr     kind
smd-machine-1-6 fixed cut    n=414   -       0.128   0.095   in-sample
smd-machine-1-6 rolling K=25 n=389   40      0.179   0.084   causal
smd-machine-1-6 rolling K=50 n=364   35      0.186   0.075   causal
smd-machine-1-6 rolling K=100 n=314  22      0.261   0.016   causal
smd-machine-2-2 fixed cut    n=414   -       0.161   0.092   in-sample
smd-machine-2-2 rolling K=25 n=389   47      0.143   0.117   causal
smd-machine-2-2 rolling K=50 n=364   43      0.125   0.117   causal
smd-machine-2-2 rolling K=100 n=314  27      0.089   0.085   causal
```

在漂移的那台主机上，K=100 用**1.6% 的误报**拿到 **26.1% 的召回**，比样本内拟合的 0.128/0.095 又好又省；而且它是因果的——每个窗口只看它之前的分数，所以这张表本身就是样本外的。在平稳但弱的那台主机上它略微更差（0.089/0.085 对 0.161/0.092），因为那里不存在漂移可供抵消，排序本身不够。

三点收束：

- **0.87（以及 0.8755 / 0.8653）不能作为常量上线。** 它是样本内数字，边界 12 现在关闭，关闭的方向是"反对固定切点"。
- **读者的可部署形态是排序规则，不是概率门限。** 这不是精度问题，是平稳性问题：一条记录内部电平摆动 0.4，任何常数都会被漂移吃掉。
- **今天的流水线还表达不了这个规则。** `judge()` 是无状态的，读者只能吐一个概率，交给 `apply_gate` 与固定的 `anomaly_probability` 比较。要让上面那张表变成产品行为，需要一个**按服务维护滚动窗口的门限**（`Pipeline`/`Evaluator` 各一份状态），这是下一轮的工作，已写进第 6 节。

### 3.21 把"最近 K 个窗口的前 10%"变成产品行为，然后被自己的一条规则挡下

3.20 节的结论是"能用的形态是排序"，可当时的代码只会吐电平。这一节把排序做进产品，并让循环去评判它。

**实现（`src/brian/scoring.py`）**：`RollingWindow(size, min_samples=ROLLING_MIN_SAMPLES)` 按服务维护 `deque`，`rank(service, score)` 先入队再算 `sum(v <= score) / len(trail)`——窗口里最响的那个得到 1.0，于是门限的 `anomaly_probability=0.90` 变成一句"比最近九成的窗口都响"。历史不足 `min_samples`（默认 10）时返回 `None`，`judge()` 退回电平，`raw["reported_as"]` 如实写 `"level"` 还是 `"rank"`。状态是**传进去的**，不是模块级全局：`Pipeline` 持有一份（`digest.rolling_window > 0` 时才建），`Evaluator.run()` 每次调用新建一份——"两条记录是两次部署，不是一段历史"。设置面：`digest.rolling_window` / `BRIAN_DIGEST_ROLLING_WINDOW`，默认 0（关），关掉时渲染与行为逐字节不变。

**候选 `host-rolling-rank`** 是一份四设置项的补丁：`digest.scoring=auto`、`digest.rolling_window=100`、`gate.anomaly_probability=0.90`、`gate.notify_probability=0.90`。（这里写 `auto` 而不是 `deviation`：在主机流上两者解析成同一个读者，本轮测量完全一样；但导出 `deviation` 会把读者一起发给服务域的文本，而那段文本从没验过。候选应当导出"在这条流上等价的、更小的"那份配置。）

```
candidate              recall@b   fpr@b   tok/rep   tok/win evidence verdict
baseline                  0.060   0.072      3350    3328.6     100% -
host-rolling-rank         0.224   0.091      3358     435.7     100% reject
  shipped threshold  baseline            recall 1.000  alerts/1k 1000  fpr 1.000
  shipped threshold  host-rolling-rank   recall 0.216  alerts/1k   89  fpr 0.089
x host-rolling-rank  the recorded configuration would page on fewer failures than the cut
                     the budget rule already computes: shipped recall 0.216 against 0.224
                     at the 10% budget
```

**这一行是错的，而且错在"规则的对象"上。** 候选把预算内召回从 0.060 提到 0.224（3.7 倍），每窗口 token 从 3328.6 降到 435.7（−87%），上线态误报 0.089。它被拒的唯一理由是：记录下来的 0.90 切点拿到 0.216，而循环自己扫出的最优切点拿到 0.224——**差 0.008，占免费切点的 3.6%**。3.6 节的 `operational` 回归规则本来是要挡"拿沉默换钱"（`anomaly-p-40`：上线态 0.007 对预算切点 0.060，差 88%），它被写成了"`not beats_own_cut(trial)`"这种**两值**判定，于是把 3.6% 的差距和 88% 的差距当成同一件事。

修法是给它一个尺寸：`own_cut_shortfall(measurement)` 返回"记录配置相对免费切点让出了多大比例的召回"，规则改为**只有让出量超过 `operational_tolerance`（默认 0.10）才算回归**，且基线本身在容差内（即这次候选是引入短差的一方）。同时删掉了阅读 `decide()` 时发现的一处**逐字重复的 `silence` 分支**（同一段判断写了两遍）。重跑后同一份测量被判 `accept quality`。

**但"被接受"立刻暴露了第二个问题，这次是我自己的验证抓到的。** 把版本文件里的导出原样加载（`BRIAN_DIGEST_SCORING=deviation`、`BRIAN_GATE_ANOMALY_P=0.9`、`BRIAN_GATE_NOTIFY_P=0.9`）再跑一次服务域合成套件：

```
recall              0.625  (95% CI [0.3057, 0.8632])
false positive rate 0.000
  recall                 0.625  (FAIL)
  ! recall=0.625 does not meet the bar (0.85)
```

默认配置下同一条套件是 **0.875**。原因不是读者也不是排序，而是**同一个数字被两种量纲共用**：在排序量纲上 `0.90` 是"进最近的前一成"，在概率量纲上 `0.90` 是"几乎确定"。补丁把排序的门限写进了 `anomaly_probability`，于是在服务域把告警门槛从 0.5583 抬到 0.90，召回掉了 0.25。这正是边界 13 说的"补丁里可以藏一个坏的一半"——只是这次藏的不是候选的一半，而是**导出被搬到另一个域时**的一半。

修法是把量纲做成显式的：新增 `gate.rank_probability`（默认 0.90，环境变量 `BRIAN_GATE_RANK_P`）。`apply_gate` 不再问配置，而是问答案：`result.raw["reported_as"] == "rank"` 时用 `rank_probability` 当存在门限与通知门限，否则用 `anomaly_probability`/`notify_probability`。补丁相应改成 `digest.scoring=auto`、`digest.rolling_window=100`、`gate.rank_probability=0.90`——**不再碰那两个概率门限**，于是同一个部署可以同时承载两个域。验证：同一份导出下服务域回到 **0.875 / 0.000**，SMD 上的测量数字一位不变（排序量纲下存在门限与通知门限同值，行为与之前完全相同）。

一个附带的语义后果要写下来：排序量纲下 `normal_probability=0.35` 也变成分位，所以"确信正常"意味着"落在最近三分之一里"，而不是"概率低于 0.35"。这是合理的（漂移流里中位数的窗口确实谈不上确信正常），但它和 `anomaly_probability` 共用字段名的做法一样，都属于"同一个名字、两种量纲"，只能靠这条文档和 `raw` 里的 `reported_as` 提醒使用者。

最终记录（`var/evolve/round-20261117T223614Z.json`、`var/evolve/versions/v011.json`）：

```
digest.scoring = 'auto', digest.rolling_window = 100, gate.rank_probability = 0.9
export BRIAN_DIGEST_SCORING=auto; BRIAN_DIGEST_ROLLING_WINDOW=100; BRIAN_GATE_RANK_P=0.9
rollback: unset BRIAN_DIGEST_SCORING  unset BRIAN_DIGEST_ROLLING_WINDOW  unset BRIAN_GATE_RANK_P

confusion tp 29 fp 79 tn 615 fn 105     shipped recall 0.216  fpr 0.114  precision 0.269
reports 138   tokens/report 3355.9   tokens/window 488.5
recall@10% 0.2239   fpr@10% 0.0908   cuts: 1-6 0.89, 2-2 0.91
```

与上面那张"被拒"的表相比，预算内召回与误报（0.224 / 0.091）完全一致——排序量纲没变——只有**上线态**那一列从 74 个误报变成 79 个、报告数从 123 变成 138：因为导出不再把概率门限抬到 0.90，只有排序门限在起作用。差的这几个窗口出在分位并列与 0.90 边界上，属于噪声量级，但两个数都如实记下来，不挑好看的那个。

三点收束：

- **容差不是放松，是把"比例"写进规则。** 0.10 是判断不是测量（和 `deploy_fpr_tolerance=0.005` 同类），它唯一的依据是"样本内拟合的切点不可能恰好最优"；边界 18 已经说过，这类常数要显式承认。
- **排序一旦进了产品，`anomaly_probability` 的含义就变了。** 0.90 不再表示"概率 90%"，而是"比最近 K 个窗口里的九成都响"。这个语义切换没有改字段名，只在 `raw` 与设置项的 docstring 里说明——这是本轮最容易被误读的一处。
- **冷启动仍走电平。** 一条新记录的窗口数不足 `min_samples` 时，`reported_as` 是 `"level"`，行为与 3.20 之前完全一致；差别只在累积够历史之后。

### 3.22 把"改了规则就要重审历史"做成一条命令

前面十四轮里，`decide()` 的规则被改过七八次，每次都有一批旧判决在语义上作废，但没有一次是自动发现的——靠的是某一轮恰好想起来了 `--rejudge`，或者等下一轮的"previous rejections"里蹦出个不对劲的理由。这正是 arXiv:2607.07663 描述的第三类问题：**版本化基础设施缺失**，判决、规则、测量三者的对应关系只存在文档和人脑里。

```bash
$ brian evolve --audit
current rules     757fd5a25275  ({"cost_tolerance": 0.05, "deploy_fpr_ceiling": 0.5, ...})
recorded rounds   22  (77 candidate measurements re-decided, no re-run)

round                      stream     recorded rules what moves today
------------------------------------------------------------------------------------------------
round-20261117T184415Z.json SMD       unrecorded     host-features-3 no-reports -> operational
round-20261117T190913Z.json SMD       unrecorded     host-features-3 fpr -> operational
round-20261117T193010Z.json SMD       unrecorded     anomaly-p-40 gain -> operational (same verdict), …
round-20261117T200320Z.json SMD       unrecorded     host-scoring-deviation quality -> deploy
round-20261117T201715Z.json SMD       unrecorded     events-8 deploy -> cost, events-4 deploy -> cost
round-20261117T214547Z.json SMD       unrecorded     host-ratio-floor-10 deploy -> recall (same verdict)
round-20261117T222244Z.json SMD       unrecorded     host-rolling-rank operational -> quality

6 verdict(s) would be decided differently today; 17 more kept their verdict but changed rule.
```

读法：

- **六条判决翻案。** 184415 / 190913 从"拒"变"收"（当年的理由是 `no-reports` 与 `fpr`，今天是 `operational` 出口）；222244 从"拒"变"收"（那是 3.21 的容差修好之前的记录）；200320 从"收"变"拒"（单字段版本今天会被上线地板挡住）；201715 的两条 `deploy -> cost` 是那条"过钝的第一版规则"的化石。
- **`unrecorded` 是诚实的空位。** 指纹是本轮才加进账本的，之前的 round 文件里没有这个字段；审计把它们标成 `unrecorded` 而不是拿今天的规则补一个上去——补上去会让旧记录看起来像是有版本号的。
- **`--strict` 才是"检查"。** 默认 `--audit` 只报告、退出码 0；加 `--strict` 时，只要有任何判决会翻，退出码就是 1。CI 里该用的是后者，否则这条命令会退化成一篇没人看的报告：
  ```
  $ brian evolve --audit --strict --min-gain 0.50 ; echo $?   # 旧账本 + 更严的规则
  1
  ```

实现上只有三件事：`rules_fingerprint(rules)` 对 `to_dict()` 排序后取 sha256 前 12 位（十二年不换手机号的那种"短"，因为要印在表格里）；`persist()` 把 `rules_hash` 同时写进 round 文件和 `ledger.jsonl`（两处都写，是因为 ledger 是索引、round 文件是正文，只有一处的话总有一边会被截断）；`audit(root, rules)` 复用 `rejudge()` 的 `Measurement.from_dict` / `Candidate.from_dict`，把每个 trial 交回 `decide()` 重判，区分"判决翻了"和"判决没翻但理由换了"。

三条边界：

- **指纹只覆盖 `EvolveRules` 的取值，不覆盖 `decide()` 的代码。** 今天加了一个 `silence` 分支、明天删掉一个，只要规则数值没动，指纹就一样，`--audit` 什么都不会说。要闭这条缝，得把 `decide()` 的源码哈希也算进去——但那需要在规则里存代码版本，属于"下一步"而不是这一轮。
- **重审只是重放数字，不是重测。** 规则如果改变的是"测什么"（比如新增一个要测的列），`--audit` 无法发现，因为旧记录里根本没有那一列。这和 3.20 的"重审不重测"是同一条限制。
- **"今天会怎么判"不等于"当时判错了"。** 200320 的 `quality -> deploy` 不是当年的错：上线地板是第 6 轮才有的规则，用今天的尺子量昨天的测量，结论是"这套目标下会怎么判"，不是"当年该判什么"。审计输出里那句 `6 verdict(s) would be decided differently today` 特意用了 would。

### 3.23 推广与复核：版本文件是一句承诺，它自己会失效吗

前面 3.12 之后的每一轮都以"接受"或"拒绝"结束，而"接受"只是对一个测量的陈述。真正会**后来才变错**的东西是另一种陈述：**这句配置在未来的数据上仍然兑现它的数字**。这一轮把这句话变成可执行的对象。

三件东西：

- **承诺（Claim）**：版本文件里那条 `measurement` 就是承诺——"在 10% 误报预算下至少召回 X，每份报告最多花 Y 个 token"。
- **生效状态（`var/evolve/current.json`）**：不是版本文件本身，而是"现在这套部署在用的环境变量"，每个变量带来源版本号。版本文件是记录，状态文件是在役。
- **复核（`brian evolve --verify`）**：把每个未复核的版本重新测量一遍，数字还兑现就留在役；兑现不了就**自动撤回**并记进版本文件。

两种结局都在真实账本上跑出来了：

```
$ brian evolve --verify --verify-version 11 --stream-file data\smd\stream-late.json
  v011  digest.scoring = 'auto', digest.rolling_window = 100, gate.rank_probability = 0.9  holds
        holds: recall 0.336 at the 10% budget against a promised 0.224,
               3355 tokens per report against a promised 3356

$ brian evolve --verify --verify-version 3            # 换了域：SMD 上立的承诺，在合成流上复核
  v003  llm.max_context_events = 8  FAILED and reverted: promised 2438 tokens per report,
        re-measured 3395 (above 2560)
```

`stream-late.json` 是按 3.20 的做法从每条记录尾部切出的 40%（380 个窗口，1-6 迟到段 69/190 故障），也就是"立承诺时还不存在的数据"。v011 在那上面召回反而升到 0.336——这正是排序规则该有的行为：电平漂移它自己跟，所以它**没有**被 3.20 证伪的那类失败。v003 则败在另一件事上：同样的配置在另一个域上每份报告贵了 39%，超出 5% 容差，于是被撤回。

**做的过程中踩到并修掉的一条真问题。** 第一次写 `apply_promotion` 时只做"合并"，于是复核一个**旧**版本（v002）会把它 0.87 的门限重新盖到更新的 v011 头上——一次伪装成复核的回滚。修法是版本序：低版本永远不从高版本手里抢变量，抢不到就报 `superseded`。测试 `test_an_older_promotion_never_takes_a_variable_from_a_newer_one` 钉住这一条。这类错误只有在"状态带来源"之后才看得见，合并字典本身不会报错。

撤回的边界很窄，也正因为窄才可信：它只移除**这个版本**当初加进去的变量，如果同名变量还有更前的版本仍然在役，就恢复那一版的值，两者都没有就删掉。任何一步都不发明一个从未被测量过的值。

三条边界：

- **"生效"不等于"热加载"。** `current.json` 是部署读的东西，但本仓库没有活的服务句柄，所以一次撤回要等进程下次启动才被看见。这是它仍然是 L2 而不是 L3 的原因（第 4 节），不是实现细节。
- **复核拿你给它的那条流。** 同一条流上是重复检验，换一条流上是迁移检验；输出里记着 `stream` 名字，但两者都叫 `holds`——这个区别只有读表的人能分辨，代码没有替读者分辨。
- **承诺只和立约时的尺子一样可信。** 承诺里的 X 是循环自己扫出来的切点给出的 X（边界 11）；如果那个切点不成立，承诺只是把一个样本内的数字写进了文件。复核能发现"数字不再兑现"，发现不了"这个数字当初就不该被当成承诺"。

---

### 3.24 让改动到达运行中的进程：热加载，以及它只覆盖一半的设置

3.23 节让"接受"变成"承诺"，让"失效"变成"自动撤回"，但两者都写在文件里。文件由进程启动时读取，而**进程一直在跑**——于是每一次接受的潜伏期都是"下一次重启"。这一轮把最后这一段接上。

`src/brian/deploy.py`：

- `RELOADABLE_FIELDS`：**能在一个正在运行的进程里换掉的字段**，逐个列出而不是按 section 一刀切。只包含"每个窗口读一次、且不拥有启动时累积状态"的设置：`gate.*`（每次决策读）、`digest.profile/host_features/scoring/host_precision/host_ratio_floor`（渲染时读）、`digest.rolling_window`（窗口重建，历史重置）、`llm.max_context_events`（取事件时读）。
- `overrides_from_state(state, settings)`：把状态文件的 `env` 分成**三类**——可热加载、**拒绝**（该进程确实拥有这个设置，但它在启动时建好了状态）、**未知**（这个构建不认识这个名字，通常是版本错配）。后两类分开报，因为只有一类是版本问题。
- `DeploymentState.poll(settings)`：用状态文件的 `updated_at` 判断"动过没有"。**比较时间戳而不是文件内容**：内容比较在空白改动上也会触发，mtime 在 `git checkout` 不改变值时也会触发；两种情况下重载都是空操作，但会被计成一次，而一个会为无关原因跳动的计数不值得打印。
- `Pipeline.reload_from_deployment()`：每次 `drain_ready()` 开头调用一次，粒度是一个窗口。换掉的设置在下一窗生效；拒绝的字段（窗口几何、基线、校准、Laya 客户端）保留启动值并**记进 `reloads`**——原因是换窗口大小不是配置变更，而是另一个实验：它会把这次运行自己 trace 里已经划好的每一个边界重新标注一遍。
- `Counters.settings_reloads`：重载次数。这个数字是"撤回真的到达了活进程"的唯一证据；它是 0 就说明这次改动还在等重启，台账上写着什么都没用。

真实演示（`var/deploy_reload_demo.py`，SMD 流，一次真实 `revert_promotion(v011)` 插在两次窗口批之间）：

```
state at start   {'BRIAN_DIGEST_SCORING': 'auto', 'BRIAN_DIGEST_ROLLING_WINDOW': '100', 'BRIAN_GATE_RANK_P': '0.9'}
before       windows 120  actions ['deep_analysis', 'observe']      reloads=1
revert now       var\evolve\current.json
state after      {'BRIAN_DIGEST_SCORING': 'auto', 'BRIAN_DIGEST_ROLLING_WINDOW': '100'}
sources after    {'BRIAN_DIGEST_SCORING': '10', 'BRIAN_DIGEST_ROLLING_WINDOW': '10'}
after        windows  60  actions ['deep_analysis', 'observe']      reloads=2

reload ...22:55:49Z  changed=['digest.rolling_window', 'digest.scoring', 'gate.rank_probability'] refused=- unknown=-
reload ...22:56:02Z  changed=['digest.rolling_window', 'digest.scoring']                        refused=- unknown=-
settings_reloads = 2
```

注意第二次重载里 `rank_probability` 不在 `changed` 里：撤回把它从状态文件里删掉了，而它回落到默认值 0.90——**恰好等于**被撤回的值，所以这一次没有可观察的变化。数值相同的撤回等于没撤回，这不是缺陷而是事实，`changed` 列表说的就是"真的动了什么"。

`tests/test_deploy.py` 用行为而不是字段来钉住这件事：同一条流水线（引擎恒定答 0.9），阈值 0.95 时动作集合与阈值 0.20 时**不同**，且后一批出现 `page_human`。字段被替换只是实现细节，动作变了才是"改动到达了进程"。

分级也随之变了：`brian evolve --grade` 现在打印 **L3**，因为"无需人工地让改动生效"这条能力由 `reload_capable()`（一个对 `Pipeline.reload_from_deployment` 是否存在的检查）给出，而不是由意图给出。

两条边界：

- **热加载只覆盖一半设置。** 窗口几何、基线窗口、校准、Laya 客户端仍然要重启；它们拥有一段"这次运行"的累积状态，换掉它们等于换一次实验。所以准确的说法不是"任何改动都免重启"，而是"**改判与改读**免重启，改窗口不免"。
- **"代码能"不等于"这个进程在跟"。** 分级看的是能力，而一个没有设 `BRIAN_DEPLOY_STATE` 的部署会一直用启动配置——它照样是"能热加载的构建"，却不是"在跟状态的部署"。唯一的可观测信号是 `settings_reloads`：它是 0 就说明这个部署没有在跟。把这两件事混为一谈，就是这一节最容易犯的自夸。

### 3.25 让循环决定"提议什么"：可变的是提问，不是判分

十六轮里有一个不说话的前提：候选目录是一份写死的列表。循环能在一堆提议里挑，却从来不能改变**提议从哪里来**。综述的 L4 正是这一步——改进机制本身可变，而改进在有限域内继续。

`src/brian/propose.py` 实现了它，并把大部分篇幅花在**边界**上而不是变更上，因为一个不受限的提议生成器，就是循环开始给自己判分的起点。边界是四条可读的声明：

- **`SPACE_FIELDS`**：哪些设置可以被提议。它由目录里已经出现过的字段 ∩ `LIMITS` 决定——一个没人声明过的字段不可达，所以"循环发现了一个我们没想到的旋钮"这件事不会靠语言模型编出来。
- **`LIMITS`**：每个数值字段的区间。区间外的邻居不提议；**没有声明区间的字段根本无法被邻居化**（`digest.scoring` 是字符串选择，没有"介于 auto 和 deviation 之间"，所以它只能由人手写进目录）。
- **邻居规则本身**：一个邻居是**同一个改动在另一个量级**，不是新维度。计数减半/加倍，小计数再加一，比率沿区间走一步（±0.05 或 ±0.10）。写成别的样子就是搜索一个没人声明过的空间。
- **`MAX_PROPOSALS`**：只限制生成的提议，不限制目录——手写的条目是有人已经论证过的断言，把它悄悄截掉会让 `--catalogue` 名不副实。

写这条规则时否掉了两个形状，都记在 docstring 里：对 `rolling_window = 100` 提 101 不是"另一个量级"而是同一个量级加误差；把 `rank_probability = 0.90` 减半成 0.45 不是"一步"而是换了个制度（变成"前 55% 都告警"）。两者都会去测一个没人提出的假设。

**成对字段按整条带子移动**（`PAIRS`）：第 5 轮已经测过拆开的代价——`digest.scoring=deviation` 把概率整体挪了刻度，单换它会让 93% 的窗口告警，修法是 0.87/0.87 这对一起动。所以生成器不单独拨 `anomaly_probability` 而留着 `notify_probability`：那是在提议"半个修复"并测一个没人提出的假设。`PAIRS` 是一份数据声明，不是隐含在代码里的常识。

**什么时候扩充空间，由账本说，不由日程说**（`mode_from_history`）：

- 最近 `EMPTY_ROUNDS = 3` 轮一届都没接受 → 目录不再付钱，这是关于**目录**的证据，不是关于设置的证据；
- 最近一轮接受了带数值的改动 → 那个量级成了"已测点"，曲线在已测点两侧弯不弯是量级问题该待的地方。

其余情况用目录：它带的是人手写的机制断言，生成的邻居没有这种东西，所以生成是退路，不是默认。

两个"想扩展但扩展不了"的退路也写明了原因：**从来没有接受过任何改动**时没有已测点可绕（"没有已测量级"），**已接受值的邻居全部落在区间外**时空间已耗尽。两种情况都退回目录，并把理由写在 `Proposal.reason` 里，而不是悄悄生成一堆凭空的候选。

**种子按流来源隔离**（`source`）：量级是**痕迹的属性**，不是流水线的属性。合成流上接受过 `reuse_report_within_s = 600`，因为那条流里同一个场景每分钟重复一次；把 600 搬到一条故障间隔二十分钟的主机记录上，是一个没人测过的提议。所以 `_accepted_measurements(rounds, source=...)`、`mode_from_history(..., source=...)` 都只读**这条流**的轮次，"最近三轮都没接受"也按这条流数。提议记录里因此多一个 `seeds` 字段——`brian evolve --propose --stream-file data\smd\stream.json` 会打印 `seeds SMD`，而一个不带来源的旧账本会打印 `seeds any stream`（那是第 18 轮之前的全部轮次）。

这条隔离是写这一轮代码时被自己的账本抓出来的：先跑了一次 demo 域的整轮（为验证渲染），再跑 SMD 的空转，发现候选里出现了 `reuse-300-600-1200` 这种"把 demo 的量级翻倍再翻倍"的名字——机制没有错，错的是它把两条流的量级当成了同一个刻度。

每一轮把**提议规则**记进 round 文件：

```
proposal          expand  space dccb2a7b0b81  (10 field(s), 13 bounded, at most 8 proposals)
why               the last round accepted host-rolling-rank, so those magnitudes are measured
                  points; the curve one step either side of a measured point is where a
                  magnitude question belongs
```

`space` 指纹是必需的，不是装饰：没有它，"机制变了"就是一句无法对照的话——两轮产生不同候选，可能因为空间动了，而记录里什么都不会说。

`brian evolve --propose` 是这条规则的干跑，**不需要 Laya**：它是关于账本的一句话，让引擎去回答只会更慢也更难核。`--catalogue` 强制回到固定列表以复现旧轮，`--expand` 强制扩充。

分级因此到 **L4**，`--grade` 也把范围写在同一行：

```
level            L4  (the proposal rule only; the objective, decide() and the evaluator stay outside)
```

第一次真正由机制提问的整轮（`var/evolve/round-20261117T230958Z.json`，SMD，8 个生成的邻居，基线 `recall@10% 0.060 / 3328.6 tokens/window`）：

| 候选 | recall@b | fpr@b | tok/win | 记录配置的 recall | 告警/千窗 | 判决 |
|---|---|---|---|---|---|---|
| `host-rolling-rank-50` | 0.187 | 0.079 | 484.5 | 0.194 | 133 | 接受（operational） |
| `host-rolling-rank-200` | **0.507** | 0.091 | 503.0 | 0.336 | 97 | 拒绝（比预算曲线少 34%） |
| `host-rolling-rank-0-95` | 0.224 | 0.091 | 286.8 | 0.127 | 79 | 拒绝（少 43%） |
| `host-rolling-rank-0-85` | 0.224 | 0.091 | 651.2 | 0.321 | 157 | 接受（operational） |
| `host-rolling-rank-0-8` | 0.224 | 0.091 | 792.6 | **0.410** | 184 | 接受（operational） |
| `…band-0-92` | 0.142 | 0.094 | 106.2 | 0.030 | 30 | 拒绝（少 79%） |
| `…band-0-82` | 0.142 | 0.094 | 622.2 | 0.306 | 169 | 接受（operational） |
| `…band-0-97` | 0.142 | 0.094 | 10.5 | 0.000 | 1 | 拒绝（少 100%） |

四接受四拒绝，而且**八个候选全都朝"告警更多"的方向移动**——因为基线在记录配置上是 1000 告警/千窗，任何把条子调低的方向都是它唯一能去的方向。这一轮真正的发现不是"哪个邻居赢了"，而是三件机制自己暴露出来的事：

1. **`rolling_window` 与 `rank_probability` 是一对，不能分开判。** K=200 的候选在预算曲线上最好（0.507，2.5 倍于基线），却因为它的记录配置（0.336）比自己的预算曲线少 34% 而被 `operational` 守卫拒绝；K=100 + 0.85 的记录配置 0.321 反而在容差内通过。同一个 K、不同的条，决定的是"记录的那一刀落在预算曲线附近还是远处"。目录里从没有表达过这个交互——是生成器把它摆上桌的。
2. **零成本被拒绝是对的，但它说明条子不能再低。** `…band-0-97` 每千窗只发 1 个告警、`tok/win 10.5`，代价低了两个数量级；它少发了 100% 的失败，所以拒绝的理由与省钱无关。
3. **一轮接受多个补丁之后，"在生效的配置"不再等于任何一次测量。** `--apply` 把四个接受的版本依次合并进 `current.json`，结果把 v012 的 `rolling_window`、v014 的 `rank_p`、v015 的 `anomaly_p/notify_p` 拼成了一个没有任何一轮测过的配置。修法分两处：

```
in force: (nothing changed)
4 patches were accepted and they are alternatives measured against the same
baseline, so merging them would produce a configuration nobody measured. Put one of
them in force with:  brian evolve --verify --verify-version <n>
```

状态文件里一旦出现来自多个版本的变量，`format_promotions` 也会自己说出来（`the variables in force come from 2 versions … a configuration nobody measured`）。这不是排版：第 16 轮给每个变量记了来源，正是为了让"合并"这件事可见，而它花了一轮才真的用上。

多个接受之间怎么选**不是循环的判决**：它们的 `recall@b` 有并列（0.224 出现三次），Pareto 比较只在同 recall 时能排开成本。这里把 `v013`（K=100、条 0.85，`recall@b 0.224`、`tok/win 651.2`）放进生效状态，并在晚段数据上复验：`holds: recall 0.336 at the 10% budget against a promised 0.224, 3354 tokens per report against a promised 3355`。

三条边界：

- **空间是手写的，循环扩充的是量级，不是维度。** `SPACE_FIELDS` 与 `LIMITS` 由人声明；循环能在区间内加减、能选规则，不能提议"试试一个新的信号源"。所以它是"有界 RSI"（L4）而不是自主研究。
- **生成出来的假设比测出来的弱。** 邻居的假设是从父候选推出来的（"值不是活性成分"），所以接受一个邻居确认的是一个**量级断言**，它不能发现新机制。把这种接受读成"循环发现了什么"，就是把一次参数搜索说成一次发现。
- **一轮里的多个接受是备选，不是叠加。** 它们对同一个基线测量，所以合并它们得到的是第四个配置；工具现在拒绝合并，但"选哪一个"仍然是人（或另一条规则）的判断——`current.json` 的来源字段只是让这个选择可见、可回滚。

---

### 3.26 迁移检验：换到服务域，标签先错了一轮

前 18 轮只在一个域（SMD，主机指标）上测量。L5 的门槛是迁移，所以这一轮把同一套循环搬到服务域（RCAEval RE1-OB：15 条记录、456 个评分窗口、279 个故障窗口），先问一个更小的问题：**这套机器在服务文本上还成立吗。**

搬过去的第一步就不是循环的问题。第一版探针在 456 个窗口里只拿到 10 个可评分的，追下去发现：`LabeledWindow.window_start` 带的是样本时间戳（`…588`，`02:16:28`），而 `Windower` 把窗口起点对齐到 epoch 的 `size_s` 网格（`…560`，`02:16:00`）。直接数了一遍：某条记录产出 70 个 digest，**0** 个能匹配上标签。

后果不是"少了一个窗口"，而是**第 2、3 轮在这条流上的判决作废**：评测器一个都没评到，表格里打印的 `recall 0.000` 不是"引擎在机会线上"，而是"标签从未命中"。当时的结论（"所有候选都被拒，因为引擎在这个域上是机会线"）读起来像一个发现，实际上是一个记账错误。

修法放在 `src/brian/streams.py` 的 `align_window_start(moment, size_s, slide_s=None)`：它复刻 `Windower._window_start` 的取整规则 `(epoch // slide) * slide - max(0, size - slide)`，适配器用它同时取整窗口起点与事件时间，并在两条记录取整后撞到同一个起点时跳过。回归测试三条：样本时间落到网格、滑动窗口按 slide 而非 size 对齐、`size_s=0` 报错而不是除零。

修好之后，同一个探针对同一个 digest 判三次：`model`（今天的服务部署）、`level`（无状态读者）、`rank`（读者 + 最近 100 个窗口的分位，即第 14 轮在主机域上接受的形态）。每格是 `AUC / 10% 误报预算下的召回`：

| 记录 | 窗口 | 故障 | model | level | rank |
| --- | --- | --- | --- | --- | --- |
| cpu_1 | 61 | 36 | 0.969 / 0.944 | 1.000 / 1.000 | 0.752 / 0.250 |
| cpu_2 | 61 | 36 | 0.974 / 0.917 | 1.000 / 1.000 | 0.752 / 0.250 |
| cpu_3 | 61 | 36 | 0.999 / 1.000 | 1.000 / 1.000 | 0.732 / n/a |
| delay_1 | 10 | 7 | 0.810 / 0.571 | 0.405 / 0.143 | 0.548 / 0.286 |
| delay_2 | 10 | 7 | 0.214 / n/a | 0.238 / n/a | 0.381 / 0.143 |
| delay_3 | 10 | 7 | 0.119 / n/a | 0.048 / n/a | 0.190 / 0.143 |
| disk_1 | 10 | 7 | 0.857 / 0.571 | 1.000 / 1.000 | 1.000 / 1.000 |
| disk_2 | 10 | 7 | 1.000 / 1.000 | 1.000 / 1.000 | 1.000 / 1.000 |
| disk_3 | 10 | 7 | 1.000 / 1.000 | 1.000 / 1.000 | 1.000 / 1.000 |
| loss_1 | 10 | 7 | 0.452 / 0.143 | 0.524 / 0.286 | 0.524 / 0.286 |
| loss_2 | 10 | 7 | 0.310 / 0.286 | 0.714 / 0.714 | 0.714 / 0.714 |
| loss_3 | 10 | 7 | 0.476 / 0.143 | 0.619 / 0.143 | 0.667 / 0.286 |
| mem_1 | 61 | 36 | 0.278 / 0.000 | 1.000 / 1.000 | 0.812 / 0.306 |
| mem_2 | 61 | 36 | 0.310 / 0.028 | 1.000 / 1.000 | 0.786 / 0.111 |
| mem_3 | 61 | 36 | 0.326 / 0.000 | 1.000 / 1.000 | 0.784 / 0.250 |

池化 456 个窗口 / 279 个故障：

| 配置 | AUC | 10% 预算召回 | FPR | p50 | p90 |
| --- | --- | --- | --- | --- | --- |
| model（今天的部署） | 0.6835 | 0.068 | 0.079 | 0.2533 | 0.2710 |
| level（无状态读者） | 0.9164 | 0.860 | 0.000 | 0.8136 | 0.9832 |
| rank（读者 + 分位） | 0.7340 | 0.244 | 0.096 | 0.5714 | 0.9524 |

三件事：

1. **机器迁移了，配方没迁移。** 读者在服务文本上照样赢引擎（池化 0.916 对 0.684，15 条记录里 7 条恰好 1.000），说明"文本可读性"这件事不是主机域的巧合。但第 14 轮在主机域上被接受的形态是 **rank**，它在服务域上反而**不如**朴素 level（0.734 对 0.916）——因为 rank 是为"电平漂移"设计的补偿（3.20 节：同一条主机记录内中位数从 0.846 掉到 0.434），而这些 60 秒服务记录是平稳的，补偿没有对象，只把 100 个窗口的历史压成了噪声。所以"循环在 A 域选的配方搬到 B 域"不是迁移，是搬运。
2. **引擎的失败也是分族的。** cpu 0.969–0.999、disk 0.857–1.000 是它能读的；mem 0.278–0.326 则是反的。这和 SMD 上 2-2 的情形同构（3.18 节）：同一台引擎、同一套提示词，在一种指标形态上排序正确、在另一种上排序反向。所以"引擎弱"这个说法太粗，准确的是"引擎在某种指标形态上不可用"。
3. **服务域的基线门限也得先修。** 引擎在这条流上的答案落在 0.24–0.28（p50 0.2533、p90 0.2710），而流继承的是 0.5——所有窗口都是 OBSERVE，循环的成本列会是空的。沿用 SMD 的先例（0.33），这里把基线改成 0.26，并写进 `BASE_OVERRIDES` 的注释：它是"跑这个域需要什么"的一部分，不是按标签调出来的最优门限。

三条边界：

- **标签的时间网格是一份契约。** 3.26 节这条错误说明：只要标签与 `Windower` 的取整规则不一致，评测器就会安静地评 0 个窗口，并打印一个看起来像结论的 0.000。现在有 `align_window_start` 与三条回归测试，但这只覆盖"起点取整"这一类；还有别的能让标签静默失效的路径（时区、夏令时、跨记录合并），本仓库没有对应的守卫。
- **一个域的配方不是另一个域的起点。** rank 在主机域被接受、在服务域更差，原因写在 3.20 与上文的平稳性差异里。要让循环自己发现这一点，提议规则必须能跨域比较——而现在的种子按来源隔离（3.25 节边界 39），正好把它挡在门外：**隔离保护了循环不串味，也让它无法自己学会"这个形态不适合那个域"**。
- **"读者更好"仍然只说排序，不说部署。** 0.916 是池化 AUC，读者在 10% 预算下拿到 0.860 召回 / 0.000 FPR 靠的还是扫一遍标签选切点（`fit_cut`）；无标签的 `quantile_cut` 与因果的 rank 都低得多，这正是 3.20 节那条边界在服务域上的重演。

修好标签之后，同一份候选目录第一次跑在服务域上（13 个候选，约 25 分钟）。基线在 10% 预算下召回 0.068 / 误报 0.079，每报告 1618 token、每窗口 646.5：

| 候选 | 判决 | 理由要点 |
| --- | --- | --- |
| `events-8` | 接受 | 1368 token/报告（−15.5%） |
| `events-4` | 接受 | 1270（−21.5%） |
| `reuse-300` | 接受 | 624（−61.4%）：这条流的重复窗口确实那么多 |
| `page-severity-3` | 拒绝 | 1654（+2.2%） |
| `analyze-pages-only` | 拒绝 | veto：它删掉的正是 `deep_analysis` 要买的那次推理 |
| `confidence-auto-85` | 拒绝 | 无变化 |
| `host-features-3` / `host-precision-3` / `host-ratio-floor-02` / `-10` | 拒绝 | 无变化：服务 profile 不读主机渲染设置 |
| `anomaly-p-40` | 拒绝 | 没有报告：0.40 高于这条流上所有答案 |
| `host-scoring-deviation` | **拒绝（cost）** | 召回 0.068 → 0.860、上线门限处误报 0，但每报告 1618 → 1703（+5.3%） |

最后一行是这一轮真正的问题：**12.6 倍的召回被 5.3% 的报告单价挡下了**。原来的成本列数的是"每个报告多少钱"，它回答的是报告的成本，不是故障的成本——一个把召回从 0.068 提到 0.860 的配置，本来会为每个故障花更少的钱。于是加了第二个分母 `tokens_per_incident(measurement)`：故障数取自 `confusion` 的 `tp + fn`，找出数 = `recall_at_budget × 故障数`，每故障 token = `tokens_per_window × 窗口数 / 找出数`。判定尾部因此多了一条：即使每报告更贵，只要每故障更便宜且幅度超过 `min_quality_gain`，就按 quality 接受。重跑该候选后 `v019` 被接受（`digest.scoring=deviation`、两条 0.87 门限）。

**同一批数据还给出了一个 SMD 上没有过的结果**：服务域的渲染确实丢信息。同一轮打印的上限表（`fields / text / engine`）：

| 记录族 | fields | text | engine |
| --- | --- | --- | --- |
| `cpu_*` | 1.000 | 0.722 | 0.969–0.999 |
| `mem_*` | 1.000 | 0.667–0.683 | 0.278–0.326 |
| `loss_*` | 0.762–0.857 | 0.571 | 0.310–0.476 |
| `delay_*` | 0.524–0.702 | 0.500 | 0.119–0.810 |
| `disk_*` | 1.000 | 1.000 | 0.857–1.000 |

在 SMD 上两列相等（3.17 节），于是当时的结论是"渲染没丢东西"；服务域上文本比字段低 0.03–0.28，说明**那条结论是 SMD 的结论，不是渲染器的性质**。对读者来说这没关系（它照样赢引擎），但它让"引擎本可以达到字段上限"这句话在服务域上不再成立——它只能达到文本上限。

### 3.27 一个部署自己算得出的门限：0.860 里有多少是"偷看答案"

3.26 节接受了 `host-scoring-deviation`，理由是召回 0.068 → 0.860。边界 43 写着：那个 0.860 是 `fit_cut` 扫着标签挑出来的切点，**部署时看不到标签**。这一轮问的就是——同一个读者，门限只能从自己的分数分布里取时，还剩下多少？

先让代码回答"这条流能取到什么门限"：

```
$ brian evolve --calibrate-bar --stream-file data\rcaeval\stream.json
stream           RCAEval RE1-OB  (543 windows)
recording                  budget        bar  windows
re1ob_adservice_cpu_1          2%     0.9834       71
re1ob_adservice_cpu_1          5%     0.9832       71
re1ob_adservice_cpu_1         10%     0.9831       71
re1ob_adservice_delay_1       10%     0.0826       13
re1ob_adservice_loss_2        10%     0.1243       13
re1ob_adservice_mem_1         10%     0.9673       71
pooled                         2%     0.9835      543
pooled                         5%     0.9834      543
pooled                        10%     0.9831      543
Each bar is the (1 - budget) quantile of these scores: the value a deployment would
need in order to alert on that share of windows without ever looking at a label.
A fitted cut needs the labels; this one needs a day of traffic. Put one of these
numbers in a candidate to measure what it costs to deploy without the answer.
```

同一批 543 个窗口（279 个故障）上，两种门限给出的东西完全不同：

| 门限来源 | 门限 | 召回 | 误报 | 告警数 |
| --- | --- | --- | --- | --- |
| 扫标签拟合（上限，不可部署） | 0.5310 | 0.860 | 0.000 | 240 |
| 分数分布分位，池化一把（可部署） | 0.9831 | 0.197 | 0.000 | 55 |
| 分数分布分位，每条记录各一把（循环实际记的） | — | 0.226 | 0.028 | — |

最后一行是循环表里的 `label-free cut`：`free_cut_rates()` 按记录各取分位，所以两条记录不共享一把尺。它比池化稍好，但仍远低于拟合上限。

读者的分数是**双峰**的：240 个故障窗口都在 0.531 以上，其余都在这条线以下。所以"10% 的告警率"这个分位落进了故障堆里——它按构造只能控制**告警频率**，不能控制**误报率**，因为它不知道哪条告警是误报。

**第一次测量因此被拒绝，而那次拒绝是 bug。** 循环复用了自己的 `beats_own_cut`：它拿"上线配置的召回"去和 `recall_at_budget`（=0.860，拟合上限）比，于是任何可部署的形态都必然判为倒退：

```
x deviation-bar-free  the recorded configuration pages on 75% fewer of the failures the cut
                      the budget rule already computes would find: shipped recall 0.211
                      against 0.860 at the 10% budget, past the 10% shortfall tolerated
```

这是 3.8 节那个错误的镜像：那里是拿"接受指标"当"上线指标"，这里是拿"上限"当"可部署参照"。修法是把两个数都记下来——`Measurement.recall_at_free_cut` / `fpr_at_free_cut`，由 `free_cut_rates()` 按每条记录自己的分位切点统计 tp/fp——`beats_own_cut()` 与 `own_cut_shortfall()` 优先读它，老记录退回它记录的旧列。

修完重跑，同一个候选被接受（`var/evolve/round-20261117T234816Z.json`）：

```
candidate            recall@b   fpr@b  tok/rep   tok/inc evidence verdict
baseline                0.068   0.079     1618     15514     100% -
deviation-bar-free      0.860   0.000     1703       352     100% accept

  shipped threshold  baseline             recall 0.405  alerts/1k    271  fpr 0.271
  shipped threshold  deviation-bar-free   recall 0.211  alerts/1k      0  fpr 0.000
  label-free cut     baseline             recall 0.136  fpr 0.130
  label-free cut     deviation-bar-free   recall 0.226  fpr 0.028

+ deviation-bar-free  recall at 10% false positives 0.068 -> 0.860, and the bill per incident
                      it finds falls 15515 -> 352 tokens even though the bill per report
                      rises 1618 -> 1703
```

三句话读这张表：

1. **不知道答案的代价是 0.860 → 0.226**。也就是说，"读者是引擎的 12.6 倍"这个说法里，大约 74% 是标签扫描贡献的。可部署的形态仍然赢，但赢的是 1.66 倍（0.226 对基线自己的可部署形态 0.136），不是 12.6 倍。
2. 赢了不止召回：上线门限处的误报从 271 个/千窗降到 0，每故障 token 从 15 514 降到 352。这两条比召回数字更值得写进运维的账。
3. 分位门限控制的是告警率，不是误报率——这条流 51% 是故障窗口，10% 的告警率对应 13.0% 的误报率。把两个预算混为一谈会得到一个"看起来守住了 10% 误报"的配置，其实只是告了 10% 的窗口。

边界 45（§5）把第 3 条固定下来：无标签门限只能按告警率定，任何用 `fit_cut` 报出来的召回都必须同时给出分位门限的结果。

**这一轮接受了一个比在役配置更差的补丁，所以没有生效。** 在役状态是 `v019`（`digest.scoring=deviation` + 两条 0.87 门限），它在服务域上的上线形态是**召回 0.807 / 误报 0.000**；本轮接受的 `deviation-bar-free` 把门限换成 0.9831，上线形态是**召回 0.211 / 误报 0.000**。它之所以被接受，是因为循环的比较基准是**出厂默认**（`load_settings()`，模型路径），而不是**在役状态**——`brian evolve` 从不读 `current.json`。于是"接受"只说明"比出厂默认好"，不说明"比线上跑的好"。本轮因此只记录、不 `--apply`，并在 §6 里把"用在役配置做基线"列为下一件事。这就是边界 46（§5）。

### 3.28 把在役配置当基线：上一轮的"接受"当场翻案

边界 46 说得很清楚，却留了个洞：循环每一轮都拿源码默认值做基线，于是"比默认好"被写成"接受"，而真正该问的是"比线上跑的好吗"。这一节把这个洞补上，并且立刻得到了一个可验证的后果——上一轮那条接受，换成在役基线后当场被否。

实现分两半。`src/brian/promote.py` 增加 `in_force_overrides(root, settings)`：读 `var/evolve/current.json`，用 `deploy.overrides_from_state` 拆成"能采纳 / 进程拿不到 / 这个版本不认识"三份，返回值里的来源表**只保留被真正采纳的变量**（被拒绝的设置不在基线里，给它贴来源标签就是在描述一个没在跑的配置）。`src/brian/cli.py` 的 `cmd_evolve` 在流文件与基线覆盖都就绪之后、测量之前调用它，并把结果 `apply_overrides` 到 settings 上；`--from-default` 保留出厂默认这条路，供对照和复现旧轮次。

命令行多打印了两类东西，都很重要：

```
in force        v019  (3 variable(s) from the deployment state)
  BRIAN_DIGEST_SCORING='deviation'  from v019
  BRIAN_GATE_ANOMALY_P=0.87  from v019
  BRIAN_GATE_NOTIFY_P=0.87  from v019
  ! the deployment replaces this domain's own base setting: gate.anomaly_probability 0.26 -> 0.87
  ! the deployment replaces this domain's own base setting: gate.notify_probability 0.26 -> 0.87
```

第一类是"在役的是什么、来自哪个版本"。第二类是**冲突**：这条流文件自己声明了 `anomaly_probability=0.26`（RCAEval 上引擎答案落在 0.24–0.28，0.5 会把整域静音），而部署状态要从另一个域带进来 0.87。两者不可能同时为真，工具现在把这句话打出来而不是让其中一条静默生效。

于是第 21 轮的基线不再是出厂默认，而是 v019 本身：

```
candidate            recall@b   fpr@b  tok/rep   tok/inc evidence verdict
baseline                0.860   0.000     1703      1341     100% -
deviation-bar-free      0.860   0.000     1703       352     100% reject

  shipped threshold  baseline               recall 0.806  fp/1k      0  fpr 0.000
  shipped threshold  deviation-bar-free     recall 0.211  fp/1k      0  fpr 0.000
  label-free cut     baseline               recall 0.226  fpr 0.028
  label-free cut     deviation-bar-free     recall 0.226  fpr 0.028
```

四句话读它：

1. **上一轮的"接受"翻案了，而且理由变了。** 第 20 轮它在模型基线上把预算内召回从 0.068 抬到 0.860；这一轮基线已经是同一台读者，预算内召回两边都是 0.860、可部署形态两边都是 0.226，于是它一文不加，判决只剩"1703 → 1703 tokens/report is below the 5% bar and recall at the budget did not rise by 5%"。判决没错，但**它一直是同一个配置**——第 20 轮接受的那条，当它成为基线的组成部分之后，就不能再作为"改进"被接受第二次。
2. **两列说同一件事，是这次翻案的机制。** `recall@b` 与 `label-free cut` 都只依赖读者的排序（3.27），而两个配置的排序完全一样（同一个 `deviation` 读者），唯一差别是打印出来的门限 0.9831 对 0.87。门限在预算列里是不可见的，所以预算列无法区分它们——这正是边界 45 说的"门限只控制告警率"的另一面。
3. **`tok/inc` 从 1341 降到 352，却没有救回这条候选。** 因为每故障 token 只在"质量提升"那条出口里被查阅，而质量提升的前提是预算内召回涨 5%，这里没涨。这是一个需要记住的形状：**第三个成本分母只在质量路径上有效**，它不会单独成为接受理由。
4. **上线列是唯一显示差异的地方**（0.806 对 0.211），但判定不读它。这不算漏洞：`silence` 规则会在"召回掉了且省了钱"时拦下；这里没省钱，所以走的是最后的 `gain` 拒绝。理由文字偏软，结论正确。

顺带修掉一处标签错误：`alerts_per_1000` 算的是**误报**每千窗（`1000 × fpr`），表格头却写 `alerts/1k`。第 21 轮里它打了 `0`，而该配置上线召回 0.211——按"告警数"读是假话，按"误报数"读是真话。现在表头是 `fp/1k`，函数文档补了这段来历。

这节留下的两个边界（§5 的 47、48）：基线变成"部署"之后，轮次之间不再可比，唯一的记录是那一份 `round-*.json`；以及**一份全局部署状态服务不了两个域**——采纳它必然让一个域的门限去管另一个域（`--from-default` 只是逃回出厂默认，不是解决办法）。

---

### 3.29 每个域一份状态，循环不再测量自己的基线

3.28 节留下一条读数：同一个 `var/evolve/current.json` 里，RCAEval 为服务域选的 `anomaly_probability=0.87` 被原样带到 SMD，而 SMD 的流文件声明 0.33 —— 两条设置不可能同时为真，工具只能把冲突打印出来。这一轮把状态按域拆开：

* `state_path(root, domain)`：`var/evolve/domains/<slug>/current.json`，`domain_slug("RCAEval RE1-OB")` 是 `rcaeval-re1-ob`。老的全局文件保留，但循环不再读它；若某个域没有自己的状态而全局文件有，`legacy_state_note` 会打印一句
  `the global state (v019, measured on RCAEval RE1-OB) is not adopted for smd: a setting measured on one stream is not a starting point on another`。
* 播种（`var/seed_domains.py`）：`smd <- v013`（`auto` / 100 / 0.85），`rcaeval-re1-ob <- v019`（`deviation` / 0.87 / 0.87）。每个域用的都是它自己那一轮接受的版本。
* `--verify` 也按域过滤（`promotions_for_domain`）：一条承诺是关于一个数据集的（边界 33），拿 SMD 的承诺去 RCAEval 上复核，量的是别人的账。

第二个缺陷是本轮自己撞上的。在役状态是 `v013`（`auto` / `window=100` / `rank=0.85`），账本里"已接受的量级"里当然就有 100 和 0.85，于是生成器把 `host-rolling-rank-0-85` 提了出来 —— 那正好就是在役配置。`--propose` 的干跑把它列成一个候选，读起来像"循环认为这个配置还值得再测一次"。

现在的规则是：**提议器知道在役配置，并把描述它的候选丢掉**。

```
1 neighbour(s) were dropped because they describe the configuration already in force
```

`propose(..., deployed=...)` 收的是和 `apply_overrides` 同形的 `{section: {field: value}}`；`_is_deployed` 只在**全部**设置都等于在役值时才丢弃（一个只改了两项中一项的候选仍是新配置）。目录条目走同一过滤器：`reuse-300` 一旦被接受，它既是手写假设又是部署，重测它等于拿基线和自己比。

本轮在 SMD 上的表格（948 窗，828 计分，134 异常；9 个候选里 1 个因"已是在役"被丢）：

```
candidate                    recall@b   fpr@b  tok/rep   tok/inc  verdict
baseline                        0.224   0.091     3355     17973  -
host-rolling-rank-25            0.157   0.088     3352     17983  reject
host-rolling-rank-50-0-95       0.187   0.079     3351      8899  reject
host-rolling-rank-50-0-85       0.187   0.079     3354     20507  reject
host-rolling-rank-50-0-8        0.187   0.079     3353     25536  reject
host-rolling-rank-0-85-200      0.507   0.091     3358      7419  accept
host-rolling-rank-0-8           0.224   0.091     3354     21875  reject
host-rolling-rank-0-95          0.224   0.091     3357      7916  reject
host-rolling-rank-0-75          0.224   0.091     3354     24994  reject
```

三条读数：

1. **K=200 是这一轮真正的旋钮。** 预算内召回 0.224 → 0.507（2.26 倍），每故障 token 17973 → 7419，无标签召回 0.209 → 0.284 且误报率 0.086 → 0.066。同侧的邻居（K=50，三个门限）全部落在 0.187，说明"更长历史"是方向而不是噪声；K=25 更差（0.157）。
2. **门限方向不能靠直觉。** `-0-8` 与 `-0-75` 在预算内召回不动（0.224），被判"成本没降、召回没涨"；`-0-95` 反而把告警缩到 shipped 0.127，被 `operational` 挡住（39% 短缺，超过 10% 容差）。三个邻居各自失败在不同的原因上——这正是表格要分开三列的理由。
3. **第三列的诚实性。** `engine` 一列这轮读到 0.833 / 0.562，而第 11 轮在同一个函数名下读到 0.517 / 0.299。差别不在模型，在配置：`scoring=auto` 已经把主机文本交给确定性读者，所以第三列现在量的是"配置指派的裁决者"。列名是历史包袱，读数必须配着在役配置看（边界 51）。

`v020` 因此写入并自动生效（单接受）：

```
export BRIAN_DIGEST_SCORING=auto; BRIAN_DIGEST_ROLLING_WINDOW=200; BRIAN_GATE_RANK_P=0.85
```

## 4. 分级：从 L2 到 L4 的三步是什么

```
level            L4  (the proposal rule only; the objective, decide() and the evaluator stay outside)
capabilities
  + proposes changes from a catalogue of hand-written hypotheses
  + evaluates them against labels, prompt evidence and a token ledger
  + records an accepted change as a version file with env and rollback
  + puts an accepted change into a deployment state file with per-variable provenance, and reverts it when its claim stops holding
  + applies an accepted change to the running configuration without a human
  + selects the proposal rule from its own ledger — the catalogue's hypotheses, or a bounded search around magnitudes that already moved a column — and records the rule, the space fingerprint and the reason with every round
missing for the next level
  - transfer beyond this stream and this declared space
```

`grade()` 把"写版本文件"、"写入部署状态"、"改动在运行中生效"、"改进机制可变"当成四个独立事实：3.23 节让前两件和"承诺失效就自动撤回"成为循环自己做的事，3.24 节把第三件接上（`Pipeline` 在窗口之间重读 `current.json`），3.25 节把第四件接上（`propose()` 从账本里选提议规则）。每一级都只由它自己那件事给出，所以"多写了几个函数"买不到一级。

这个 L4 是**有界且被声明范围**的，而且范围写在打印出来的那一行里：`(the proposal rule only; the objective, decide() and the evaluator stay outside)`。可变的是"问什么"，不变的是"怎么判"——`decide()` 的规则、评测器的两条流、目标函数都不在循环手里。一个能同时改写自己的判分规则的循环，可以在不改任何设置的情况下把判决变好看，那就不是改进了。

`grade()` 刻意不读文件系统、只收事实参数：一个自己报告自己等级的评估器，是自证循环的第一个例子。


---

## 5. 边界（不能宣称的）

1. **不改进检测。** 结构上 System 2 在 `publish decision` 之后才跑，token 花多花少改不了 recall/FPR。循环能优化的只有分析成本与证据完整性。
2. **解释质量没被测。** 无 API key，reasoner 是 `offline`；`coverage` 是"有没有把证据喂进去"的代理，不是"报告写得好不好"。
3. **目录条目是手写的，目标函数是固定的。** 3.25 节让"提议规则"可变（目录 vs 有界扩充），但两条边界还在循环之外：目录里的**假设**仍由人写（生成器只能在已声明字段的区间内做量级邻居，边界 37），目标函数与判定规则仍由人定。这就是它停在 L4 而不是 L5 的原因，也是它不变成自证循环的原因。
4. **真实数据上的第一次接受，只有一半被两层证据支撑。** 3.10 节接受了三个候选：`host-features-3` 由标签列（0.060 → 0.187）加成本列同时支撑；`events-8`/`events-4` 只由成本列支撑，因为这条流的证据列结构性失效（无日志可丢）。同时，接受发生在"两台主机的合计"上，而机器 2-2 单独低于机会线；在它被解释清楚之前，这些数字不能外推成"SMD 可检测了"。
5. **"接受"的含义有限。** 它只说明：在同一条流、同一个评测器、同一份证据池上，一个配置严格优于另一个，且没有丢掉评测器看得见的东西。
6. **真实域的静音可能再次来自接线。** 第 3.5 节已经抓到三个这样的 bug；`digest_specs`/`baseline_keys` 的测试挡住了同一类错误，但挡不住别的（例如未来新增的域需要新的目录或新的基线 key）。
7. **quality 这条接受路径只在一条流上验证过一次。** 它由 3 台以上的记录共同决定，而 SMD 只有两台主机：机器 1-6 有可分性（0.674），机器 2-2 低于机会线（0.347），合并后的 at-budget recall 主要由 2-2 决定。所以即使 `host-features-3` 被接受，被接受的也是"两台主机的合计在 10% 误报预算下多捞到一些故障"，不是"SMD 被解决了"。
8. **阈值仍然不是循环能改的。** `anomaly_probability=0.33` 写在流的基线配置里（`scripts/real_data/smd_stream.py` 的 `BASE_OVERRIDES`），因为它是这个域的部署前提，不是候选。循环用来比较的是 at-budget 曲线，它不负责把这条曲线接到线上门限。
9. **at-budget 指标对"只动阈值"的候选是盲的，3.14 节用第三列补上了。** 曲线本身是把观测到的概率重扫一遍，所以 `anomaly-p-40`（把门限从 0.33 抬到 0.40）在两条记录上算出完全相同的 recall/FPR。3.14 节的 `operational` 路径改用"记录下来的配置"做判定，于是这类候选第一次可判；但实测结论是**没有一个纯阈值候选在这条流上通过**——不是规则太严，是引擎在 8 特征上的排序接近机会线（3.7 节 AUC 0.511）。
10. **状态上限说明"文本里有信号"，不等于"部署能在这个点上告警"。** 3.11 节的读者是一个阈值规则，它的 AUC 只说明排序可分；把它换成线上门限还需要每主机标定、以及误报预算下的实际页面量。3.12 节把这条改动做成了候选并让它被接受，但接受的仍然是一个"在 10% 预算下多捞到故障、并且在 shipped 门限处可用"的配置，不是"SMD 可检测了"。
11. **接受指标与上线指标仍然是两条列，只是多了一道地板和一条出口。** 3.13 节的 `deploy_fpr_ceiling` 让"shipped 门限处会告警多少"进入了判定，3.14 节的 `operational` 让它也能单独接受一次改动；但两条都只读 shipped 那一列，at-budget 曲线仍然是排序质量的唯一证据。所以"接受"依旧不蕴含"上线可部署"，也不蕴含"检测更好"。
12. **读者自己的 0.87 是在同一批窗口上选的，3.20 节已把它证伪成"不能上线的常数"。** 时间上拟合一半、评估另一半，结果是 0 告警或 100% 召回 / 55.1% 误报；跨主机迁移"看起来可以"只是因为两条记录带着同一种不平稳。能用的形态是"最近 K 个窗口的前 10%"这类排序规则（1-6 上 K=100 得 0.261 召回 / 0.016 误报，因果、无标签），但把它接进流水线需要按服务的有状态门限，本仓库还没有。
13. **补丁式候选可以藏一个坏的一半。** 成本列看的是整份补丁，一项降低告警量的伴随改动可以掩护另一项抬高误报率的改动；3.13 节的部署地板挡掉的是"整体不可部署"，挡不住"其中一半是坏的，但另一半把合计拉回线内"。
14. **0.50 这条部署地板是判断，不是测量。** 没有数据支持"误报超过一半即不可部署"这个具体数字；它只挡住了离它很远的配置（3.12 节的 0.935）。把它变成测量需要每页告警的人工成本，本仓库没有这个输入。
15. **重审只重放数字，不重跑。** `--rejudge` 回答的是"今天的规则会怎么判这份记录"，不是"今天的配置今天测出来是什么"；后者要重跑（SMD 全目录约 9 分钟）。
16. **"电平回到文本里"不等于"引擎读得懂"。** 3.15 节修掉了一个真缺陷（2-2 的 CPU 0.0016 被打印成 0.00，电平完全没进文本），修完 2-2 的 AUC 从 0.297 起到 0.436，仍然远低于同一段文本的 0.597；1-6 几乎不动（0.523/0.538 对 0.827）。所以"文本为空"这个混杂因素被排除了，结论反而是：**引擎在这类状态上的排序能力本身不足**。这一条边界是为了防止下一轮把"修渲染"误当成"修检测"。
17. **加宽小数只在 0.01 以下生效，这是判断。** 阈值取 0.01 是因为从 0.01 起两位小数已经给出两位有效数字，而 0.01 以下的固定两位会直接归零；它不是从数据里扫出来的最优切点。0.0016 与 0.00160 哪个读数更好，只有 3.15 节那次池化测量回答过一次（答案：没有差别），更多位数没有测。
18. **删掉噪声比值也没能让引擎读懂。** 3.16 节把 2-2 上 40% 的"近零基线比值"从 digest 里拿掉，剂量反应是负向的：floor 0.02 收益为零，floor 0.10 让召回从 0.060 掉到 0.045。所以"引擎失败是文本质量问题"这个假设的两条主要路径（打印电平、删除噪声比值）都已被实测否定。
19. **3.11 的上限原本只是"字段"的上限，3.17 已量成"文本"的上限。** 上限与 `digest.scoring=deviation` 的读者都读结构化字段，而引擎只读渲染出来的句子，所以这一层信息差曾被列为未知。3.17 节加了只读渲染文本的 `text_deviation` 并在同一批窗口上重算：0.827/0.827、0.597/0.598、0.886/0.886、0.516/0.521——两列相等，说明渲染没有丢信息。于是'引擎本可以达到 0.827'这个说法现在成立，但它仍然只说排序可分，不说能在生产门限下告警（见边界 10）。
20. **`deploy_fpr_tolerance = 0.005` 是判断。** 没有数据支持"千分之五以内算同一个部署"；它只挡住了 0.999 与 1.000 这种明显不该算作新主张的情形。真要测量需要"多少窗口的告警差异会让运维认为行为变了"，本仓库没有这个输入。
21. **引擎在主机 digest 上给出的不是检测，是偏置。** 3.18 节的三次替换：改名不改答案、放大幅度不改答案、把比值全部抹平反而让答案升高（1-6 上 0.2360 → 0.2487，2-2 上 0.2271 → 0.2570）；3.19 节又用五个渲染 × 两个问句复现了同一件事（没有一种形状在两条记录上都赢）。一个"读到更多信号就更容易说异常"的读法无法产生这样的结果，所以这台引擎在这类文本上的输出不能解释成概率意义上的异常度。它仍然可以在服务形态的 digest 上工作（本仓库合成套件 0.875 召回 / 0.000 误报），失败是**这种文本形状**的失败，不是模型全都不会。这条边界的作用是：不许把 0.517/0.299 再拿去做阈值标定、特征筛选或成本优化，那些都不会改变偏置。
22. **3.19 的格子是"最强窗口"上的可读性，不是部署流行度。** 样本按打印出的最大偏离均衡挑选（每类 100 个），所以在自然分布上引擎的数字会更低（3.18 的 0.517 / 0.299 对 3.19 的 0.6062 / 0.4704）。引用时必须带上样本口径；n≈180 时 AUC 标准误约 0.045，格子中大部分数值落在 ±2σ 内。
23. **"换问句就跳 0.17"说明这条流水线的答案由提示词决定的那一部分大于状态贡献的那一部分。** 这不是说改问句能修好它——本轮的三个问句没有一个把四个臂都拉起来；它说的是在本仓库当前这台上，主机状态的边际值不值得再调提示词，值得的是换裁决者（`auto`）。
24. **`auto` 是分工，不是改进。** 它把主机状态交给读者、服务状态留给模型，实测在服务路径上逐位不变、在 SMD 流上等同于 `deviation`。它没有提高任何一条流的召回，也不构成"主机检测已解决"。
25. **排序一进产品，`anomaly_probability` 的含义就变了，而字段名没变。** 3.21 节把 `digest.rolling_window>0` 时的答案改成分位（0.90 = "比最近 K 个窗口里的九成都响"），语义切换只写在 `raw["reported_as"]` 和设置项的 docstring 里。任何按"概率 90%"读这个数字的外部消费者都会读错，而本仓库没有能强制这一点的接口。
26. **`operational_tolerance = 0.10` 是判断。** 依据是"样本内拟合的切点不可能恰好最优"，不是扫描出来的；它把 3.21 节那个 3.6% 的短差从"回归"改成"落在容差内"，换任何 0.05–0.2 的值，本轮结论都一样，但未来某个候选的判决会随它翻转。
27. **排序规则只在累积够 `min_samples`（默认 10）个窗口后才生效。** 一条新记录的前 9 个窗口仍按电平判（`reported_as="level"`），所以"上线即用排序"这句只在稳态成立；冷启动段的可比性没有被单独测量。
28. **一个数字不能同时是概率和分位。** 3.21 节把这条踩实了两次：补丁把排序门限写进 `anomaly_probability` 后，服务域召回从 0.875 掉到 0.625；修法是新增 `gate.rank_probability` 并让 `apply_gate` 按 `raw["reported_as"]` 选门限。字段名没有变，量纲随答案走——任何把 `anomaly_probability` 当"唯一存在门限"读的外部消费者，在排序部署上都会读错。

29. **指纹只覆盖规则取值，不覆盖 `decide()` 的代码。** 3.22 节的 `rules_fingerprint` 只哈希 `EvolveRules.to_dict()`；同一组数值下增删一个分支（比如 6.4 节那个 `silence` 规则怎么排）不会改变指纹，`--audit` 也就不会说话。要闭这条缝，得把判定函数的版本一起记进账本。
30. **`--audit` 是重放数字，不是重测。** 规则如果改变的是"测什么"（新增一列、换一个预算），旧记录里没有那一列，审计只能沉默。这和 3.20 的"重审不重测"是同一条限制。
31. **"今天会怎么判"不等于"当时判错了"。** 3.22 节那六条翻案里，`200320` 的 `quality → deploy` 用的是第 6 轮才引入的上线地板：审计输出用 `would be decided differently today` 而不是 `was wrong`，因为目标变了，不是测量变了。
32. **"生效"不等于"热加载"。** 3.23 节的 `current.json` 是部署启动时读的东西；本仓库没有活的服务句柄，所以一次自动撤回要等进程下次启动才被看见。这条边界是它仍为 L2 的原因——把状态写对不构成"无需人工"。
33. **复核的结论依赖你指向哪条流。** 同一条流上是重复检验，换一条流上是迁移检验；两者在表格里都叫 `holds`，只有 `stream` 字段能区分。代码没有替读者分辨这件事。
34. **承诺的可信度上限就是立约时那把尺子的可信度。** 承诺里的召回是循环扫出来的切点上的召回（边界 11 的同一条限制）：复核能发现"数字不再兑现"，发现不了"这个数字当初就不该当作承诺"。
35. **热加载只覆盖一半设置。** 3.24 节的 `RELOADABLE_FIELDS` 只列每窗口读取、且不拥有启动时累积状态的字段：`gate.*`、`digest` 的渲染与排序设置、`llm.max_context_events`。窗口几何、基线窗口、校准、Laya 客户端改了要重启——它们拥有一段"这次运行"的状态，换掉等于换一次实验；所以准确的说法是"改判与改读免重启"，不是"任何改动免重启"。
36. **"这个构建能热加载"不等于"这个部署在跟状态"。** 分级检查的是代码路径（`reload_capable()`），而一个没设 `BRIAN_DEPLOY_STATE` 的进程会一直用启动配置。唯一可观测的信号是 `Counters.settings_reloads`：为 0 就说明它没在跟。把能力当成事实，是 3.24 节最容易犯的自夸。
37. **可变空间是人声明的，循环扩充的是量级，不是维度。** 3.25 节的 `SPACE_FIELDS` 与 `LIMITS` 由人写定；循环能在区间内加减、能选提议规则，不能提议"换一个新的信号源"或"试试另一个指标"。所以它拿到的是 L4（有界 RSI），不是自主研究。任何"循环发现了我们没想到的旋钮"的说法，在这份代码里都不可能成立——那个旋钮必须先被人写进 `SPACE_FIELDS`。
38. **生成出来的假设比测出来的弱。** 邻居的假设是从父候选推出来的（"这个值不是活性成分"），不是从机制推出来的。接受一个邻居因此只确认了一个**量级断言**，它不可能发现新机制；`--audit` 里那些"邻居被接受"的记录，读法必须是"量级不敏感"而不是"循环想出了新办法"。同一条限制也解释了为什么生成是退路：目录里手写的假设带机制，邻居没有。
39. **量级不能跨流迁移，3.25 节已把种子按来源隔离。** 一条流上测出的 600 秒是那条流的属性；换一条记录，故障间隔与重复模式都变了。代码现在按 `stream.source` 过滤种子，但这条规则只保护"生成"这一步：手写目录里的绝对值（例如 300 秒复用）依旧被两条流共用，因为那是人给出的假设，不是从数据里读出来的。
40. **一轮里的多个接受是备选，不是叠加；合并它们得到的是第四个配置。** 3.25 节那一轮接受了四个补丁，`--apply` 曾把它们依次合并进 `current.json`，于是 `rolling_window` 来自一个版本、两条阈值来自另一个版本，合成出一个没有任何一轮测过的配置。现在 `--apply` 只在恰好接受一个时自动生效，多个时打印"它们是同一基线上的备选"；状态文件里出现跨版本变量时 `format_promotions` 也会报警。但**"选哪一个"仍是判断**：`recall@b` 并列时只有成本能排开，而"用多少误报换多少召回"是产品口径，不是循环的判决。

41. **标签的时间网格是一份契约，违约时评测器保持沉默。** 3.26 节在 RCAEval 上量到了最坏的情形：标签带样本时间戳、digest 带网格时间戳，70 个 digest 命中 0 个标签，而表格打印的是 `recall 0.000`——一个读起来像"引擎在机会线上"的数字。修法是 `align_window_start`，但那条守卫只覆盖"起点取整"这一类；时区、夏令时、跨记录合并都还能让标签静默失效，本仓库没有对应的检查。所以**任何一次 `recall 0.000` 都要先问标签是否命中**，再问引擎。

42. **一个域的配方不是另一个域的起点。** 3.26 节把同一套机器搬到服务域：读者照样赢引擎（池化 0.916 对 0.684），但第 14 轮在主机域接受的形态是 rank，它在服务域上反而更差（0.734 对 level 的 0.916），因为 rank 补偿的是电平漂移，而 60 秒服务记录是平稳的。种子按来源隔离（边界 39）保护了循环不串味，同时也挡住了它自己学会"这个形态不适合那个域"——跨域比较目前只能由人做。

43. **"读者更好"仍然只说排序。** 服务域的 0.916 是池化 AUC；10% 预算下的 0.860 召回 / 0.000 误报来自 `fit_cut`——扫一遍标签选出切点。无标签的 `quantile_cut` 与因果的 rank 都低得多（0.244 召回），这是 3.20 节那条边界在服务域上的重演，也提醒：把"读者 AUC 高"读成"可以直接上线"，等于把样本内拟合当成部署能力。

44. **补丁栈不是配置。** 3.26 节的 `--apply` 把服务域接受的 `v019`（`digest.scoring=deviation` + 两条 0.87 门限，测量时 `rolling_window=0`）叠在主机域留下的 `v013`（`rolling_window=100`、`rank_probability=0.85`）之上，在役状态因此变成"读者 + 排序"，而那句承诺是在"读者 + 电平"上测出来的。`INTERACTIONS` 现在是一份手写的成对声明（`scoring`↔`rolling_window`、`rolling_window`↔`rank_probability`、`anomaly_probability`↔`notify_probability`），`--apply` 遇到继承来的相互作用项会打印 `!` 警告；但"哪一对会互相改变含义"是人列的，设置之间没有依赖模型，所以这份名单永远可能不完整。真正安全的做法是**一次只让一个版本的整份补丁在役**（本轮已按此重建状态：`var/evolve/current.json` 只含 `v019` 的三个变量）。

45. **无标签门限只能按告警率定，它的召回是"你得到多少"，不是"你要多少"。** 3.27 节把读者的分位门限和拟合切点放在同一张表上：0.9831 的分数分位给出召回 0.197 / 误报 0.000 / 55 条告警，而同一条流上扫标签的 0.5310 给出 0.860 / 0.000 / 240 条。差别不是"调参没调好"，而是**信息**：分位门限不知道哪条告警是误报，所以它只能控制告警频率；在 51% 是故障窗口的流上，10% 的告警率对应 13.0% 的误报率，两个预算因此不能共用一个数字（现在仍共用 `fpr_budget`）。**任何用 `fit_cut` 报出来的召回，都必须同时给出同一条流上的分位门限结果**，否则那个数字描述的是数据，不是部署。

46. **循环的基线是出厂默认，不是在役配置。** 3.27 节接受的 `deviation-bar-free` 上线形态是召回 0.211，而在役的 `v019` 是 0.807——它被接受只因为比较基准是 `load_settings()`。`brian evolve` 从不读 `current.json`，所以"接受"现在的意思是"比出厂默认好"，不是"比线上跑的好"；`--apply` 的守卫只挡组合、不挡倒退。本轮的处理是**只记录不生效**（没有写 `v020`），把"以在役状态为基线重测"留给下一轮——它同时会逼出一个设计问题：`current.json` 是全局一份，而两个域需要的门限不同。

47. **基线一旦是"部署"，轮次之间就不再可比。** 3.28 节把 `current.json` 接进 `cmd_evolve` 之后，同一个候选在两轮里的判决可以相反，原因不在候选也不在规则，而在基线移动了（第 20 轮接受的那条，第 21 轮被同一条流判为"不改进"）。因此**每一轮的 `round-*.json` 是唯一能说明"当时比的是什么"的记录**，而 `rules_hash` 只覆盖规则、不覆盖基线；跨轮比较必须同时读 `baseline.recall` 与在役版本号。想复现旧轮次，得显式 `--from-default`。这条限制没有修法——部署在变，基线就该变——但任何"第 N 轮比第 M 轮好"的说法都必须先说明两次的基线是谁。

48. **一份全局部署状态服务不了两个域。** 3.28 节的冲突行是证据：RCAEval 的流文件声明 `anomaly_probability=0.26`（引擎答案落在 0.24–0.28），而在役状态为 SMD 选的 0.87 被原样带进来，两条设置不可能同时为真，工具只能把冲突打印出来。这不是 `--from-default` 能解决的——那只是逃回出厂默认。真正的解法是让状态按域（或按 `stream.source`）分文件，`--verify` 也要相应地只复核它所属域的承诺；本仓库还没有这一步，所以现在**跨域跑循环得到的是"某个域的配置在另一个域上怎样"，不是"这个域应当配置成什么"**。

49. **"在役"现在带一个限定词：哪个域。** 状态按 `stream.source` 分文件之后，`--verify` 不给流文件就无从复核，`current_state(root)` 的默认值仍是老的全局文件、只是没人读它。因此"这个配置在役"这句话，必须能回答"在哪个域上"；跨域引用一条版本号，等价于引用一份别的数据集的承诺。

50. **提议器能生成自己的基线。** 3.29 节的过滤器是"完全匹配"：一个只有一半设置与在役相同的邻居仍会被测一轮，目录里的绝对值也可能正好等于部署。把 `deployed` 传进去只是让循环不重复已知答案，不等于让每一轮都有信息量——后者取决于候选目录本身还剩多少未测的机制。

51. **第三列量的是"配置指派的裁决者"，不是模型。** `scoring=auto` + 主机流时，`state ceiling` 的 `engine` 列读的是确定性读者的输出（3.29 节 0.833），与第 11 轮 `scoring=model` 下的 0.517 不可比。把两轮的这个数字放在一张表里比较，就是在比较两个不同的裁决者；列名没有改，是为了不假装历史读数可以重算。

---

## 6. 下一步（按验证强度排序）

1. **（3.17 已完成）上限已经是文本上限。** 两列相等，那条含糊关掉了。
2. **（3.18 已完成）逐窗分层做完了，答案是一个偏置。** 改名、放大、抹平三种替换都指向同一件事：引擎在这类文本上不读信息。因此"继续在候选目录里调参"这条路被关闭——下一个动作不是试配置，而是**改喂法**。
3. **（3.19 已完成）"换措辞能不能救"已被五渲染 × 两问句回答：不能。** 同一批主机数字渲染成五种形状、问两个问题，四臂横跨 0.17–0.67 且方向在两条记录之间翻转，控制组（无数字文本）精确落在 0.5000。于是落地的不是新渲染，而是分工：`digest.scoring="auto"` 按 profile 选裁决者（主机 → 确定性读者，服务 → 模型）。由此剩下的是一条**产品决策**而非实验：主机状态在线上是否交给读者，以及是否接受它在 10% 误报预算下的 0.142 召回（3.12、3.19 的边界 24）。
4. **（3.22 已完成）"改了规则就重审全部历史"已经是一条命令。** `brian evolve --audit` 把 22 轮、77 个候选测量在今天的目标下重判一遍：6 条判决会翻，17 条判决不变但理由换了；`--strict` 时有翻案就以退出码 1 结束，这才让它成为 CI 里的一道门而不是一篇报告。剩下的缺口是边界 29：指纹管不到判定代码本身。
5. **（3.20 / 3.21 已完成）读者切点的"标定"问题被换成了"平稳性"问题。** 不是给每台主机各选一个常数，而是发现常数在这条流上不可用：记录内部电平摆动 0.4、类别均值差 0.03，所以固定切点在时间上不迁移。3.21 节把排序规则接进了产品（`RollingWindow` + `digest.rolling_window`，`Pipeline`/`Evaluator` 各一份状态），候选 `host-rolling-rank` 被接受：预算内召回 0.060 → 0.224，每窗口 token −87%。剩下的仍是产品决策：**要不要在生产上把主机状态交给读者**，以及上线后谁来监控它的漂移（这条规则会随流量自我调整，所以"告警率突然上升"可能是流量变了而不是故障变了）。
6. **真模型校准**：配 API key 后重跑一轮，把 `coverage` 与真实解释质量对比，看这个代理到底代理了什么。
7. **（3.23 / 3.24 已完成）`apply` 真生效，分级到 L3。** "在役状态"（`current.json` 带来源版本号）、"自动回滚"（复核失败即撤回）、"不需要人工重启"（`Pipeline` 在窗口之间重读状态，`Counters.settings_reloads` 为证）三件事都齐了，所以 `--grade` 从 L2 变成 L3。但它有两条边界：**只覆盖每窗口读取的设置**（窗口几何、基线、校准、Laya 客户端仍要重启，边界 35），以及**"能热加载的构建"不等于"在跟状态的部署"**（没设 `BRIAN_DEPLOY_STATE` 的进程照旧用启动配置，边界 36）。下一个 L3 之后的问题是：谁来启动这个进程、谁看 `settings_reloads`。
8. **（3.25 已完成，有界）让 `Imp` 可变，分级到 L4。** 提议规则现在从账本里选（目录 vs 有界扩充），每一轮记录 mode、空间指纹、`seeds` 与理由；可变的是**问什么**，`decide()`、评测器与目标函数仍在循环之外（边界 37/38）。种子按流隔离（边界 39），所以一次扩充只在这条流上有意义。剩下的路是 L5 的**迁移**：把同一套循环搬到第二个域（RCAEval 的服务指标与 SMD 的主机指标已经是两种域，但候选目录只服务后者）而不重写机制。

9. **（3.26 已完成，两个域）迁移检验做了第一遍，结论是"机器迁移、配方不迁移"。** 服务域上读者照样赢引擎（池化 0.916 对 0.684），但主机域接受的 rank 形态在平稳的服务记录上更差（0.734 对 level 的 0.916）；引擎的失败按指标族分裂（cpu/disk 能读，mem 反向）；标签时间网格的缺陷让第 2、3 轮在这条流上的判决作废。同一轮还补上了第二个成本分母（每故障 token），让"召回涨 12.6 倍但每报告贵 5.3%"的候选第一次能被接受，并把"渲染丢信息"从 SMD 的结论降级成一条域相关的事实。剩下的路：给服务域一份自己的候选目录（现在的目录只服务主机形态），以及一个能跨域比较"哪种形态适合哪个域"的机制——后者被边界 39 的种子隔离挡在门外，需要显式打开。

10. **（3.27 已完成）无标签门限进了表格，也进了判定。** `brian evolve --calibrate-bar` 用读者自己的分数分布给出 2/5/10% 告警率对应的门限；每一轮现在同时打印 `recall@b`（拟合上限）与 `label-free cut`（可部署形态），`Measurement.recall_at_free_cut` 让 `beats_own_cut`/`own_cut_shortfall` 拿可部署的参照去判"是否倒退"。同一批数据上 0.860 → 0.226，说明 12.6 倍里约 74% 是偷看答案。剩下的路：把"告警率预算"与"误报率预算"分开建模（现在 `fpr_budget` 一个数字同时被两种门限使用，边界 45），以及给每条记录单独标定告警率（现在的分位是按记录算的，但预算仍是全局一个）。

11. **（3.28 已完成）循环的基线现在是在役配置，而上一轮的接受因此翻案。** `cmd_evolve` 先读 `var/evolve/current.json`，按 reloadable 字段采纳它、把"进程拿不到/这个版本不认识"的设置分别打成 `refused`/`unknown`，并在与流文件自带的域基线冲突时打印 `!`。结果是第 20 轮接受的 `deviation-bar-free` 在它自己的域上被判为"不改进"——基线已经是同一台读者，预算内召回与可部署召回两边都一样，差别只剩一个在预算列里不可见的门限。这一轮同时修掉 `alerts/1k` 这个标签错误（它算的是每千窗误报，不是告警），并留下边界 47（基线移动使轮次不可比）与 48（一份全局状态服务不了两个域）。剩下的路：**让状态按域分文件**（`current.json` → `<source>/current.json`，`--verify` 只复核所属域的承诺），以及把"每故障 token"从质量路径里放出来，让它在成本路径上也能单独成为接受理由（本轮的 `tok/inc` 1341 → 352 就因为召回没涨而被忽略）。

12. **（3.29 已完成）状态按域分开，提议器不再测量自己的基线。** `var/evolve/domains/<slug>/current.json` + `domain_slug` + `promotions_for_domain` 让"在役"有了域限定词；`--propose` 与真正的轮次都会把"描述在役配置"的候选丢掉并计数。本轮在 SMD 上接受了 `host-rolling-rank-0-85-200`：预算内召回 0.224 → 0.507，每故障 token 17973 → 7419，无标签召回 0.209 → 0.284。剩下的路：**给每个域一份自己的候选目录**（现在目录只服务主机形态，服务域的那四个渲染候选全是"no change"），**把告警率预算与误报率预算分开建模**（边界 45），以及**让每故障 token 在成本路径上也能单独成为接受理由**——本轮的 1341 → 352 就是因为召回没动而被忽略。
