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

## 3.30 两个预算不是同一个预算：告警率、误报率，以及"分数重复"这个坑

第 45 条边界说：标签无关的门限只能控制*告警率*；而 `fpr_budget` 一直在兼任两件事——既限制"偷看标签的扫描"，又决定"不看标签的条"。在一半窗口都是故障的流上，这两个数字不是一回事：

```
告警率 10% 最多能找出   10% / 61.2% = 16.3% 的故障      （RCAEval RE1-OB，故障占比 61.2%）
告警率 10% 最多能找出   10% / 16.2% = 61.8% 的故障      （SMD，故障占比 16.2%）
```

所以 `EvolveRules` 现在有两个数：`fpr_budget=0.10`（允许扫描曲线花掉的误报率）和 `alert_budget=0.10`（`digest.scoring=deviation` 时那条条限允许翻页的窗口比例）。`Measurement.label_free_ceiling` 就是上面那个商，表里叫 `of at most`。

两个域的在役配置实测（`var/probe_alert_budget.py`，无标签口径）：

| 域 | 故障占比 | 看标签的上界 | 无标签召回 | 上限 | 在役 recall |
| --- | --- | --- | --- | --- | --- |
| SMD | 0.162 | 0.507 | **0.269** | 0.612 | 0.403 |
| RCAEval RE1-OB | 0.612 | 0.860 | **0.136** | 0.143 | 0.860 |

### 上限抓出的缺陷：0.226 > 0.163

第一版的结果是 RCAEval 的无标签召回 **0.226**，比它自己的上限还高（按名义 10% 预算是 0.163，按它实际花掉的 14.9% 算是 0.149）——一条"只翻 10% 窗口"的门限不可能做到这件事。算回来：命中 0.2258 × 279 ≈ 63，误报 0.0282 × 177 ≈ 5，合计 68 次告警 / 456 个窗口 = **14.9%**。

原因是分数太少：`deviation_probability = dev / (1 + dev)`，而 `dev` 来自两位小数的比值，所以大量窗口共享同一个分数。朴素的 `(1 - budget)` 分位数落在那个共享值上，一翻就是几十个窗口。

修法在 `label_free_cut`：从高到低走不同的分数值，取第一个"落在它之上（含）的窗口数 ≤ `max(1, round(n × budget))`"的值；若连最高分都被太多窗口共享，返回 `None`——一句"这条流撑不起这个告警率"，而不是一条撒谎的门限。`Measurement` 同时记 `alerts_at_free_cut` / `scored_at_free_cut` / `free_cut_alert_rate`，表里新增 `alerts 82/828` 这一列，让"预算"和"实际花掉的"并排出现。

修好之后：

| 域 | 实际告警 | 比例 | 无标签召回 | 上限 |
| --- | --- | --- | --- | --- |
| SMD | 82 / 828 | 9.9% | 0.284 → **0.269** | 0.612 |
| RCAEval RE1-OB | 40 / 456 | 8.8% | 0.226 → **0.136** | 0.143 |

SMD 几乎没变（比值连续，几乎没有重复分数）；RCAEval 掉到上限之内，也就是说第 20 轮那个"无标签召回 0.226"里有 40% 是这条门限多翻了窗口买来的。

### 上限用的是"实际花掉的比例"，不是预算

第 53 条边界的另一半：`label_free_ceiling` 里的分子取 `free_cut_alert_rate`（真实翻页比例），取不到才退回 `alert_budget`。原因是小样本撑不起名义比率——15 个窗口的流在 10% 的预算下只能翻 1.5 个窗口，实际翻了 2 个，花掉 13.3%。用名义预算去比，会把"这条流根本没能力遵守这个比率"报成"门限违纪"。演示域就是这样：无标签召回 0.250，上限按名义预算是 0.188（看似违纪），按实际花掉的是 0.250（恰好用满）。

### 三点读法

1. **上限不是目标**。`of at most` 写的是"这条门限不可能超过"，不是"应该达到"。SMD 的 0.269 距上限 0.612 还差很远，绑住它的是排序质量而不是预算；RCAEval 的 0.136 则贴着样本量的地板——10 个窗口的录像在 10% 的告警率下只能翻 1 个窗口。
2. **告警率控制的是一次数，不是一条比率**。分数有原子值时，"翻 10% 的窗口"这句话没有实现方式：要么翻整个原子，要么一个都不翻。这就是第 52 条边界。
3. **两个预算必须同时打印**。只打印 `fpr_budget` 时，RCAEval 的无标签召回看起来是 0.226（一个不可能的数字）；只打印 `alert_budget` 时，SMD 的上限 0.612 看起来像个目标。两者并排，读者才能看出哪个数字在撒谎。

## 3.31 让"每次事件的花费"单独成为成本出口

第 6 节第 13 项：第 22 轮接受的那条候选把 `tok/inc` 从 17973 压到 7419，可这个数字只在质量路径上被读——报告变贵、召回也变的时候，它救不了候选。第 19 轮服务域那次正是如此：`host-scoring-deviation` 把召回从 0.068 抬到 0.860，代价是每份报告 1618 → 1703（+5.3%），超过 5% 的容忍，于是被判 `reject cost`，而"每找到一次故障"的花费从 **15516 掉到 1341**。

现在 `decide()` 里多了一个独立出口，位置在"质量涨、账单不涨"之后、"质量涨、账单超容忍"之前：

```python
recall_holds  = 预算内召回未下降
paging_holds  = 在役召回未下降          # 见下面那次泄漏
if recall_holds and paging_holds and 每次事件花费下降 >= min_gain:
    接受，rule = "cost"
```

规则的说法是：探测器的计价单位是"找到的事件"，不是"写出的报告"；只要每条事件的账单按成本门槛下降、且没有靠少翻页换来，这一次花费就算降了。对照第 21 轮之前的口径——报告单价是*产出*的价钱，事故单价才是*检出*的价钱。

### 用旧记录验证

`brian evolve --rejudge var\evolve\round-20261117T232141Z.json`（第 19 轮那次服务域完整测量）：

```
host-scoring-deviation reject cost            accept cost            -> accept

host-scoring-deviation was rejected: recall improves but the bill rises 1618 -> 1703
                                tokens/report, more than the 5% a quality gain may cost
                       now  accepted: the bill per incident it finds falls 15516 -> 1341 tokens
                                with recall at the 10% budget 0.068 -> 0.860, even though
                                the bill per report moves 1618 -> 1703

1 verdict(s) changed. The stored measurements were not re-run; only the rules moved.
```

这正是第 13 项说的那个判决，翻案的依据是当年就记在账上的 `confusion`——不需要重跑。

### 泄漏：便宜的事故单价可以靠"少翻页"买到

第一版没有 `paging_holds`，`--audit` 立刻显示第 21 轮的记录从 `gain` 变成 `cost` 被**接受**了：

```
round-20261117T235604Z.json RCAEval RE1-OB   deviation-bar-free gain -> cost
```

原因：`deviation-bar-free` 与在役配置共享同一套排序，所以 `recall_at_budget` 两边都是 0.860（预算内召回对"只动门限"的候选天然不变），而每份报告的价钱也几乎相同（1703.2 对 1703.3）；但它只翻前 10% 的窗口，于是在役召回从 0.806 掉到 0.211，报告数从 225 掉到几十份，**总开销**跟着掉——按事件算就是 15516 → 1341 的"便宜"。

所以门槛必须同时看两列：预算内召回（排序是否变差）和**在役召回**（部署上是否少翻页）。加上 `paging_holds` 之后：

```
7 verdict(s) would be decided differently today; 18 more kept their verdict but changed rule.
```

8 → 7，去掉的正是那条假便宜。

### 两点读法

1. **同一个"接受"可能有两个名字**。先命中"质量涨、账单没涨"就叫 `quality`，先命中"事件单价下降"就叫 `cost`；名字描述的是理由，不是功劳大小。第 19 轮那条测试因此从断言 `rule == "quality"` 改成 `rule == "cost"`，并在注释里写清顺序。
2. **事件单价没法替代在役那一列**。`recall_at_budget` 对门限类候选是不变量（3.14 节），所以"找到多少事件"必须再用实际翻页那一列兜一次底；这也是第 54 条边界。

## 3.32 把 35 轮汇总成一张表：战役报告，以及它不能声称的东西

到这一轮为止账本里有 35 条记录、130 个候选测量、46 次接受，但**没有任何一处能回答"这些轮次一共买到了什么"**。每一轮都能说清自己拒绝了谁，却没有一个视图把它们并排放；调研里把这种状态叫 scientific amnesia——行为在变，方法论不留痕。本节加的不是新机制，而是一张从已有记录里读出来的表。

### 读什么，不读什么

新模块 `src/brian/campaign.py` 只做汇总，不做测量：不调用 Laya、不重跑任何一轮（"它打印的每个数字都是某一轮测出来的，所以它没法用一次自己挑的新测量来给自己贴金"）。三个来源：

- `var/evolve/round-*.json` + `ledger.jsonl`（`history()`）：每轮测了哪条流（`stream.source`）、候选的判决规则（`cost` / `quality` / `operational` / `gain` / …）、哪一轮写入了版本；
- `var/evolve/versions/v*.json`（`load_promotions()`）：每份版本承诺了什么（`claim`）、是否被复核过（`verification`）、是否被自动回滚；
- `var/evolve/domains/<slug>/current.json`（`current_state(domain)`）：每个域现在真正在役的变量与来源版本。

命令是 `brian evolve --campaign`（`--json-out` 输出同一批数字）。**流字段在第 3 轮才出现**，所以那之前的一轮无法归属；它被放进 `(unattributed)` 一行并标注"no stream recorded"，而不是猜一个域——猜出来的归属会让总数和账本对不上。

### 这一轮的真实输出

```
rounds            35  (130 candidate measurements, 46 accepted, 84 rejected)
applied           10 round(s) wrote versions; 1 version(s) were reverted
audit             7 verdict(s) would be decided differently today, 18 relabelled
unattributed      1 round(s) predate the stream field

domain             rounds trials accept in force  shipped claim
------------------------------------------------------------------------------------------------
SMD                    18     80     26 20        recall 0.507 at the 10% budget, 3358 tokens/report
  accepted by       cost x9, operational x8, quality x9
  rejected by       deploy x6, fpr x2, gain x16, no-reports x13, operational x7, recall x4, veto x6
  in force          BRIAN_DIGEST_ROLLING_WINDOW=200 BRIAN_DIGEST_SCORING=auto BRIAN_GATE_RANK_P=0.85  (unverified)
demo                    9     23     13 -         no claim recorded
  accepted by       cost x10, operational x3
  rejected by       gain x10
RCAEval RE1-OB          7     25      6 19        recall 0.860 at the 10% budget, 1703 tokens/report
  accepted by       cost x3, quality x3
  rejected by       cost x1, gain x8, no-reports x6, operational x2, veto x2
  in force          BRIAN_DIGEST_SCORING=deviation BRIAN_GATE_ANOMALY_P=0.87 BRIAN_GATE_NOTIFY_P=0.87  (unverified)
(unattributed)          1      2      1 -         no claim recorded  (no stream recorded)
  accepted by       cost x1
  rejected by       evidence x1
```

### 四点读法

1. **接受率不是交付率。** 46 次接受，只有 10 轮写出了版本，而真正在役的只有两份（SMD 的 `v020`、服务域的 `v019`）。中间的差额不是浪费，而是"同一轮里的多个接受是备选、不是叠加"（边界 40）——但把 46 读成"有 46 项改进"就是错的，这张表第一次把这个差放在同一页上。
2. **拒绝才是多数，而且原因可数。** SMD 上 80 次测量里 `gain` 16、`no-reports` 13、`operational` 7、`veto` 6、`deploy` 6。最有信息量的是 `no-reports`：那一列不是"候选没用"，而是这条流在该配置下一份报告都不产生，成本列无从比较（第 2、3 轮就是被它掩盖的）。
3. **两个域的"在役"都还是 `unverified`。** 复核只在第 16 轮跑过 `v002`/`v003`/`v011`/`v013`；`v019` 与 `v020` 是后来接受的，从来没拿新数据回测过。这张表把这句话显式印出来，而不是留给人去翻 `versions/`。
4. **`demo` 有 13 次接受、零承诺。** 合成流是规则试验场：`cost` 规则在那里被反复验证过（x10），但它没有任何版本的承诺，也没有域状态。把它单列一行，是为了让"这不是生产结论"变成一个可见的事实，而不是一段注释。

### 它不能声称什么

这张表是**已记录主张的索引**，不是"战役改善了系统"的证据。每个数字都由写下它的那一轮测出，唯一今天被重算的是 audit 那一列（7 条会翻、18 条换理由）。想让它变成证据，缺的是同一份配置在固定留出集上跨轮的可比测量——而这正是边界 47（基线移动）与 33（承诺依赖数据集）在挡的东西。

## 3.33 复核要看形状：一次采样不同的复核把健康配置回滚了

第 25 轮的战役报告（3.32）把一张表放在一页上，表里写着：真正在役的只有两份版本，而且两份都是 `unverified`。这一轮去补那次复核。

**RCAEval 拿到的是真留出集。** `scripts/service_data/rcaeval_stream.py` 新增 `--offset N`：每个故障族跳过前 N 个案例，于是 `--per-fault 3 --offset 3` 取的是第 4–6 个案例，早期任何一轮都没测量过。15 条记录、456 个计分窗口、279 个故障窗口。复核结果：

```
v019  digest.scoring = 'deviation', gate.anomaly_probability = 0.87, gate.notify_probability = 0.87  holds: recall 0.857 at the 10% budget against a promised 0.860, 1706 tokens per report against a promised 1703
```

召回差 0.4%、成本差 0.2%：确定性读者在它没见过的案例上兑现了承诺。

**SMD 那边则是一次误判。** 为了不重复用同一段数据，`smd_stream.py --stride 7` 生成了 `data/smd/stream-check.json`（1355 个窗口，1235 个计分，192 个故障），然后：

```
v020  digest.scoring = 'auto', digest.rolling_window = 200, gate.rank_probability = 0.85  FAILED and reverted: promised recall 0.507 at the 10% budget, re-measured 0.297 (below 0.457)
```

复核器做了它被造出来要做的事——而这个回滚是错的。**原因在机制里**：`v020` 的承诺依赖"最近 200 个窗口"的排名，而一个窗口代表多少真实时间取决于采样方式。声明这条承诺的那一轮用 `--stride 10`（保留窗口相隔 50 分钟），复核用 `--stride 7`（相隔 35 分钟）。同一个数字 `200` 在两个流上不是同一个时间段，于是 0.297 与 0.507 的差别里混进了采样差别，而承诺里没有任何东西记录过它。

**修法是把"形状"变成复核的前置条件。** `src/brian/promote.py` 新增 `GEOMETRY_TOLERANCE = 0.25` 与 `_geometry_mismatch(recorded, checked)`：只比较形状（计分窗口数与每窗事件数），不比较结果；超出 ±25% 就返回一句话，而不是一个判决。`claim_check` 在比较任何召回之前先看它，命中就返回 `unmeasurable`：

```
v020  …  unmeasurable: the re-check measured 1235 scored windows against the 828 the claim was made on (1.49x), so this is a different measurement rather than a worse one: a window means a different amount of traffic when the stream is sampled differently
```

在同一形状上复核（`data/smd/stream.json`）则精确复现：

```
v020  …  holds: recall 0.507 at the 10% budget against a promised 0.507, 3358 tokens per report against a promised 3358
```

于是 `--verify` 有三种结局，而不是两种：`holds` 写记录并把版本放进状态；`failed` 写记录并回滚；`unmeasurable` **只写记录、不碰部署状态**——"在一个未知上应用"与"在一个未知上回滚"一样错。

**记录本身也有第二个缺陷。** 好的复核把 `holds` 写回去之后，版本文件里仍留着坏复核写的 `reverted_at`，表格就会对着**正在运行**的配置说"已撤回"。`write_verification` 现在把旧的 `verification` 追加进 `checks` 列表（这份承诺被怎么检验过，属于记录的一部分），写新的，并在新记录不是失败时清掉 `reverted_at`；表格给这样的版本加一句 `(N earlier check)`。需要说明的是：`v020` 那次失败的检查发生在 `checks` 存在之前，所以它的历史从这一轮开始——这恰好是"为什么要有历史"的证据，而不是可以补写的空白。

同一处还修了域过滤：没有记录流的早期版本**保留**在每个域的列表里并标上 `(no stream recorded)`（先前被整体丢掉，导致 `v003` 那次真实撤回在唯一复核过它的域里消失）；有域时表格再补一行，说明有多少条记录属于别的流、要用它们的流去复核。

---

## 3.34 窗口数不是时长：把采样密度写进承诺，把"拒绝"变成数字

第 26 轮修好了复核的前置条件，但留下两个尾巴：**承诺本身没有记录采样密度**，以及"判不出来"只给了判不出来的理由，没给下一步。这一轮补上这两点，用的还是 `v020` 那次误判的数据。

**同一个窗口数，不是同样的流量。** 两条 SMD 流直接量出来的结果：

```
data/smd/stream.json         windows   948  scored   828  cadence   3000 s  span  2838000 s (788.3 h)
data/smd/stream-check.json   windows  1355  scored  1235  cadence   2100 s  span  2841300 s (789.2 h)
```

总时长几乎一样（788.3 小时对 789.2 小时），窗口数差了 1.49 倍——差别全在采样密度。于是 `rolling_window = 200` 在两条流上分别覆盖 600000 秒与 420000 秒的流量：**复核算的排名只看了 70% 的时间跨度**。0.297 与 0.507 的差因此有机械解释，不需要假设模型退化。

**记录密度。** `src/brian/streams.py` 的 `summarise()` 现在额外汇出两个字段：`cadence_s`（相邻窗口起点间隔的中位数）与 `span_s`（窗口覆盖的总墙钟时间）。加上原有的窗口数、事件数，一份轮次记录第一次能完整描述"这段数据长什么样"。

**比较密度。** `_geometry_mismatch()` 在窗口数与每窗事件数之后增加第三道比较：两边都有 `cadence_s` 且相对差超过 `GEOMETRY_TOLERANCE`（25%）时，判词直接说清差在哪：

```
a window covers 2100 s of traffic here against 3000 s when the claim was made (0.70x cadence)
```

**拒绝要给数字。** 新增 `rescale_hint(promotion, recorded, checked)`：当 `digest.rolling_window` 这类"窗口数"旋钮存在时，算出覆盖同样墙钟时间所需的窗口数，并在 `--verify` 的 `unmeasurable` 分支打印：

```
  v020  digest.rolling_window = 200  unmeasurable: a window covers 2100 s ... (0.70x cadence)
        hint  a comparable re-check on this stream would use BRIAN_DIGEST_ROLLING_WINDOW=286
              (200 windows x 3000 s is 600000 s of traffic, the same span 286 windows covered here)
```

这个数字**只报告、不应用**：改了窗口数就是换了一个版本，复核结果将属于那个新版本，而不是被复核的这一份。它的用途是让下一轮**有意识地**提出那个版本，而不是让人猜"200 个窗口"在另一条流上意味着多久。

**战役报告一起修。** `--campaign` 现在把复核结局分开计数（`verification 2 holding, 0 not comparable, 0 never re-checked`），在役版本那一行打印 `(holds)` / `(not comparable)` / `(unverified)`，并把历史检查次数写成 `(holds, 1 earlier check)`。顺带修掉一个计数缺陷：被撤回的版本在 `campaign()` 里被加了两次，而其中一次在"没有记录流"的分支之后才执行——`v003` 那种早期版本因此一次都不计。真实账本上现在读作 `1 version(s) were reverted`。

三点读法：（1）**密度是承诺的一部分**——一个"最近 N 个窗口"的承诺，N 在没有密度的情况下不可比；（2）**拒绝也必须可执行**，判不出来时给出"怎样才判得出来"，否则 `unmeasurable` 就是死胡同；（3）`v020` 自己的记录里没有 `cadence_s`（它是第 22 轮写的，那时还没有这个字段），所以它的复核仍然只能按窗口数判——这个修法是**向前生效**的，历史记录不补写。

### 3.35 测试这个解释：0.297 里有多少是窗口几何

3.34 节给出的仍然是一句**解释**："密度不同，所以排名只看了 70% 的时间跨度。" 解释是可以被证伪的，而且只需要一次测量：把同一个版本放到 hint 给出的窗口数上再跑一遍，看它回到多高。不做这一步，就等于把"我认为原因是几何"当成"原因是几何"——这正是本文档反复批评的那种自证。

**实验**（`var/probe_hint_experiment.py`，两条真实流，同一个 `measure`）：

```
v020: digest.scoring = 'auto', digest.rolling_window = 200, gate.rank_probability = 0.85
承诺的几何: scored 828  cadence 3000 s        复核的几何: scored 1235  cadence 2100 s
hint: BRIAN_DIGEST_ROLLING_WINDOW=286 (200 x 3000 s = 600000 s)

承诺那条流  window 200  recall@b 0.507  fpr@b 0.091  reports 172
承诺那条流  window 286  recall@b 0.396  fpr@b 0.092  reports 141
复核那条流  window 200  recall@b 0.297  fpr@b 0.092  reports 221
复核那条流  window 286  recall@b 0.427  fpr@b 0.080  reports 219
```

**读法一：解释成立了一大半，但不是全部。** 换到等价窗口数后，`v020` 在复核流上从 0.297 恢复到 **0.427**，即 0.210 的缺口里 0.130（62%）是窗口几何。剩下的 0.080 回到那条承诺流的自身特性上：`200` 这个值当初是**在承诺流上被测得最好**才被接受的，所以承诺里本来就掺了"这条流的采样"这一项。**承诺是配置与被选流共同的性质**，不是配置单独的性质。

**读法二：等价窗口数是可比性工具，不是最优值。** 在承诺流上，286 反而更差（0.507 → 0.396）。所以 hint 的作用是"让两次测量量同一个东西"，绝不能当成"应该把在役窗口改成 286"的建议——后者需要在这条流上重新做一轮，而不是从一次换算是推出来的。

**测量装置**：`src/brian/promote.py` 的 `rescaled_candidate(promotion, recorded, checked)` 返回"只改窗口数、其余设置不变"的候选（名字带 `@hint286`），`shape_note()` 打印它与承诺的比值。`--verify --at-hint` 把这个候选送进同一个 `measure`，结果写进该版本 `verification` 的 `shape` 字段，而判决仍是 `unmeasurable`：

```
  v020  ...  unmeasurable: the re-check measured 1235 scored windows against the 828 ...
        hint  a comparable re-check on this stream would use BRIAN_DIGEST_ROLLING_WINDOW=286
              (200 windows of 948 is the same share of the recording as 286 of 1355)
        shape  the same version at rolling_window=286 recalls 0.427 on this stream, 84% of the promised 0.507
```

`shape` 不应用、不回滚：窗口数不同就是另一个版本，不能让被复核的这一份继承它的结果。但**它必须能被看见**——只打印在终端里的拒绝仍然是死胡同。所以 `format_promotions()` 在版本行下面重打这一行，`--campaign` 的表格里也就能看到"这份在役承诺在可比形状上是什么样"。

**hint 对早期记录仍要给出数字。** v020 写在 `cadence_s` 存在之前，第一版 `rescale_hint` 因此对它返回 `None`——恰恰是最需要的那个版本拿不到 hint。现在两边都有 `cadence_s` 时按密度算，否则按**窗口数之比**算（`recorded.windows / checked.windows`），因为拒绝判词本来就引用了同一个比值（"1235 对 828，1.49 倍"）。两种口径在这条数据上给出同一个 286，措辞分别说明用的是哪一种：

```
BRIAN_DIGEST_ROLLING_WINDOW=286 (200 windows x 3000 s is 600000 s of traffic, ...)
BRIAN_DIGEST_ROLLING_WINDOW=286 (200 windows of 948 is the same share of the recording as 286 of 1355)
```

**顺带修掉一个记录卫生缺陷。** 调试拒绝时把同一条 `--verify` 跑了三遍，`checks` 于是攒出三个一模一样的 `unmeasurable`，表格读作 `(4 earlier checks)`。`write_verification()` 现在只在判决、理由或 `shape` 发生变化时才把上一条存进 `checks`：重复的复印件不是历史。

### 3.36 复核会过期：一份判不出来的承诺不能永远悬着

`--verify` 只挑 `pending` 的版本复核，而 `pending` 的定义是 `verification is None and not reverted_at`。这留下一个洞：**`unmeasurable` 不是 pending**——它有一份判词，只是判词说"这两个数不可比"；一份半年前的 `holds` 也不是 pending。两者都不会再被挑中，于是"我们知道我们不知道"在记录里只真实了一天，之后就冻结成一张永久证书。战役表把今早的 `unmeasurable` 和一年后的 `unmeasurable` 记成同一行，读者无从分辨。

修法分三处，少一处这个洞就还在：

1. **算年龄**：`verification_age_days(verification, now=None)`。没有时间戳或时间戳读不出来就返回 `None`（不知道年龄就按"未复核"处理，不按"新鲜"处理）；**没有时区的时间戳按 UTC 读，不按"现在"读**——把旧时间当成刚发生，正是让过期判词看起来新鲜的机制。
2. **判到期**：`Promotion.due(now, days)` = 没被回滚，并且（从未复核 **或** 判词超过 `CLAIM_EXPIRY_DAYS = 14` 天）。`--verify` 的选择条件从 `item.pending` 换成 `item.due(days=args.expiry_days)`，被重新挑中时先打印原因：

```
verifying 1 promotion(s) from smd on SMD
  v020  re-checking: its last check is less than a day old (expiry 0 day(s))
  v020  ...  unmeasurable: the re-check measured 1235 scored windows against the 828 ...
        hint  a comparable re-check on this stream would use BRIAN_DIGEST_ROLLING_WINDOW=286 ...
        shape  the same version at rolling_window=286 recalls 0.427 on this stream, 84% of the promised 0.507
```

3. **报出来**：`format_promotions()` 在版本行后面加 `(checked 3 days ago)`，过期时加 `, overdue`；`--campaign` 多一行计数，且在役行标 `(holds (overdue))` / `(not comparable (overdue))`。`--expiry-days` 让这条口径可以被显式收紧（演示用 0.001 天，即 86 秒）：

```
$ brian evolve --campaign
verification      1 holding, 1 not comparable, 0 never re-checked          (没有过期行)

$ brian evolve --campaign --expiry-days 0.001
verification      1 holding, 1 not comparable, 0 never re-checked
expired           2 of them are older than 0 day(s): the record stands, the evidence does not
  in force  BRIAN_DIGEST_ROLLING_WINDOW=200 ...  (not comparable (overdue), 2 earlier checks)
  in force  BRIAN_DIGEST_SCORING=deviation ...   (holds (overdue))
```

**演示时又抓到一个缺陷：一次普通复核会删掉 shape。** 只有 `--at-hint` 会测第二形状，所以不带这个开关重跑一次，`verification.shape` 被写成 `null`，战役表上那行 `shape` 就消失了——**一个真被测出来的数字，因为后来的一次复核没重复测它而消失**。这和本轮的主题是同一件事：数字是真的，只是它比判词旧。修法是"携带并标注"：新判词没有 shape 而旧判词有时，把旧 shape 带过来并记 `shape_from`，打印成

```
        shape  the same version at rolling_window=286 recalls 0.427 on this stream, 84% of the promised 0.507 (measured 2026-11-18 01:08)
```

新判词自己测到 shape 时不带 `shape_from`（那才是"刚刚测的"）。注意这里的取舍：不是把旧数字当成新数字，而是让它以**自己的日期**留在页面上，判词与测量各自带上时间。

**边界 63：判词的时效属于判词，不属于版本。** 一份 `holds` 说"那天我去看过，它还在"，不说"它永远在"。版本继续在役是正确的（回滚一次健康的配置代价更高），但"证据有效"必须有一个期限，否则战役表就在替一份 2024 年的检查背书。

**边界 64：重新复核不等于重新裁决。** 到期的 `holds` 被重新挑中，仍然走同一条 `claim_check`：过就继续在役、不过才回滚、量不了就只刷新记录。到期只改变"多久看一次"，不改变"看到什么算通过"——否则"过期"就变成了一个新的、偷偷降低门槛的判定条件。

### 3.37 把提示变成提案：同一条承诺，两种判法

第 27 轮在判不出来时算出"等价窗口数是 286"，第 28 轮用 `--at-hint` 真测了它、把它记成"同一承诺的另一形状"。两轮都停在**报告**：数字在页面上，循环不会自己去测它，更不会把它当成一次改动。第 30 轮把 hint 变成提案（§6 第 18 条的第一条路），而做这件事立刻暴露一个判定问题——**同一条改动，拿什么当基线？**

拿在役的 0.297 当基线，0.427 是一次 +44% 的提升，`quality` 会接受；但 0.427 是"没人承诺过的数字"，而版本当初承诺的是 0.507。拿承诺当基线，问题才回到本来的样子：**这个配置在可比采样上，还能不能兑现它当初的承诺？** 于是候选多带两个字段（`promise` 与 `provenance`），`decide()` 给它们一条独立规则 `reshape`：

```
reshape         v020 at window 286: the span its claim was made over
  v020's own settings at the window count that covers the same span (200 windows x 3000 s is 600000 s
  of traffic, the same span 286 windows covered here)
candidate                              recall@b   fpr@b  tok/rep   tok/inc  verdict
baseline                                  0.297   0.092     3356     11861  -
host-rolling-rank-0-85-200@hint286        0.427   0.080     3356      8170  reject
host-rolling-rank-0-85-200@hint286-0-9    0.427   0.080     3356      6229  accept
```

```
x host-rolling-rank-0-85-200@hint286 reaches 0.427 of the promised 0.507 at the 10% budget, so part of
  the claim was a property of how the stream was sampled: this restores comparability, not the promise,
  and the deployment stands
+ host-rolling-rank-0-85-200@hint286-0-9 recall at 10% false positives 0.297 -> 0.427 for 3356 -> 3356
```

两条判决同时出现并不矛盾：**"承诺回收不了"是拒绝，"在这条流上更好"是接受**，它们回答的是不同问题，账本把两个理由都留下。

跑这一轮的代价是四个缺陷，每一个都是跑出来的，不是读出来的：

1. **承诺没到判定手上。** 第一次真跑（`round-20261118T011544Z.json`）把 reshape 记成 `rule: "quality", accepted: true`——分支根本没进。原因：`decide()` 读 `trial.candidate.promise`，而 `Measurement.candidate` 只是候选的**名字**字符串。承诺必须跟着测量走：`Measurement` 多一个 `promise` 字段，而且必须是**最后一个**字段——插在 `candidate` 后面会让"非默认参数跟在默认参数之后"，13 个测试文件在 collection 阶段就失败。
2. **一份过期的种子。** 第二次跑开始围绕那个"被接受"的 reshape 生成邻居（`@hint286-0-9/-0-8/-0-75`），每条都以 0.427 对 0.297 被 `quality` 接受。原因：`_accepted_measurements()` 相信账本里的判决，而那份判决是**旧规则**做出的。`--audit` 早就知道这件事，提议路径却不知道——现在它用今天的规则、从记录自带的 `baseline` 重判每一条接受（`promise` 缺失时从候选补回），与 `--audit` 共用同一套判断。
3. **跨密度的量级不是种子。** 记录里的 `-143`/`-572` 等量级来自另一条采样的流。`_same_shape()` 先比 `cadence_s` 再比 `windows`（任一侧没有就视为相同，让第 27 轮之前的记录仍可用），不同密度的接受不再当种子；全被过滤时理由写成"这段采样上没有任何被接受的改动，量级不跨密度"。
4. **换个名字的同一条改动。** 生成的邻居 `@hint286-0-85` 与 reshape 的改动完全相同，于是 `quality` 接受了承诺规则刚刚拒绝的那个配置。修法是在生成邻居之前把 reshape 的改动放进 `known`：**同一个配置只出现一次，且由它自己的问题来判。**

还有一个顺手抓到的小缺陷：`Measurement.from_dict()` 没读 `promise`，所以任何"重判一条历史记录"的路径都会把承诺丢掉——而这正是第 2 条缺陷的修法所依赖的字段。默认值不发明证据，但也不该丢掉已经记录的字段。

**边界 65：承诺按承诺判，不按基线判。** 拿基线判 reshape，循环会报告一个没人承诺过的数字并把它当成改进；而只按承诺判，循环会拒绝一次真实的改进（0.427 > 0.297）。两条规则并存才诚实：账本同时记 `reshape` 的拒绝与 `quality` 的接受。

**边界 66：账本里的"接受"是当时的规则的意见。** 任何以历史接受为输入的机制（种子、目录、提议空间）都必须先用自己的规则重判一遍，否则一条已撤销的规则会通过种子继续繁衍——`--audit` 会告诉你哪条历史接受了、哪条今天不接受，但那只是一张表；提议路径必须自己重判。

**边界 67：在另一种采样密度下测出的量级不是种子。** 窗口数是几何量，含义随采样改变（第 27 轮）；把另一密度的最优量级搬过来，等于用一个换了单位的数继续做实验。

---

### 3.38 同一几何才能相减

第 31 轮要修的不是一个坏候选，而是**比较本身**。第 30 轮在 `data/smd/stream-check.json` 上接受了一个把窗口从 200 改到 286 的候选，理由是 recall@b 从 0.297 涨到 0.427（+44%）。但同一轮里那个只改窗口、什么都不动的 reshape 候选也测到 **0.427**——那 +44% 全在几何上，不在检测能力上。

原因在 `run_round`：它对所有候选只测一次基线（`measure(settings, None, ...)`），`decide()` 就拿这个基线去比每一个候选。候选把窗口从 200 换成 286 时，基线仍然是 200 上的 0.297，于是"这个候选比基线好"实际在说"286 这个几何比 200 这个几何好"。

修法只有一句：**用候选自己的几何去测基线**。`configuration_window(settings, candidate)` 把候选套到设置上读出滚动窗口；窗口不同就测一个 `baseline@<window>`（只改 `digest.rolling_window` 的候选），每个窗口只测一次，与候选一起写进轮次记录（`references`）。表里于是多出几行：

```
baseline                0.297   0.092     3356     11861     100% -
baseline@143            0.219   0.097     3355     16164     100% reference
baseline@286            0.427   0.080     3356      8170     100% reference
baseline@572            0.135   0.054     3351     19973     100% reference
```

同一张表里，第 30 轮被接受的三个"邻居"（`@hint286-0-9`、`-0-8`、`-0-75`）在 286 的基线下全部变成平手，理由一致地是 `3356 -> 3356 tokens/report is below the 5% bar and recall at the budget did not rise by 5%`。它们从来不是三个改进，而是**同一次几何改动的三种写法**；差别只在 shipped 阈值那一列（0.260 / 0.417 / 0.536）。

**第二处缺陷：种子。** 修好基线之后，下一轮仍然围着这三个"被接受的"邻居长出三个新候选，每个都要花一次测量。原因是 `_accepted_measurements()` 用记录里的基线重判——而记录里的基线还是 200 那个，于是旧的接受原样复现。修法是让这类接受不能当种子：候选改动了 `digest.rolling_window`、而它的记录里没有那个窗口的 `references`，它就不是"本几何下的测量点"，跳过；还要说清为什么，否则一份满是接受的账本会读成"这里什么都没被接受过"：

```
why the last round on SMD accepted host-rolling-rank-0-85-200@hint286-0-8-572, so those
magnitudes are measured points — but the 10 accepted magnitude(s) at window(s) 286 were
decided against a baseline at another geometry and their round measured no reference at
their own, so they are not measured points here and the catalogue stays the source
```

**第三处：这一轮的基线测不了这条承诺。** 表格上方现在先印承诺本身，用的还是第 26 轮那条三结局判定：

```
in force          v020 promised recall 0.507 at the 10% budget
on this stream    recall 0.297  unmeasurable
                  the re-check measured 1235 scored windows against the 828 the claim was made on (1.49x) ...
                  so this round's baseline is not the configuration the promise is about: a gain
                  over it is a gain on this sampling, not a restored promise (boundary 68).
```

三处都进记录：轮次 JSON 有 `references` 与 `claim`（版本、承诺、本轮测到、裁决、原因、年龄、是否到期），所以下一轮能重放"当时用哪个几何比了什么"。修好之后同一条流上跑出两轮：第一轮（`round-20261118T012841Z.json`，references 143/286/572）只在 572 的基线下接受一个 `operational` 候选（92 → 114 告警/千窗，召回 0.073 → 0.115）；第二轮（`013236Z`，references 100/286）提议来源退回目录，15 个候选里接受 `events-8`（−27.2%）、`events-4`（−36.3%）、`host-features-3`（−13.4%）与两个成本出口（每事件 11861 → 11060 / 11007）。

**边界 68：会移动测量几何的改动，只能记它在两边共同几何上的增量。** 一个候选把窗口从 200 换到 286 时，0.297 → 0.427 是采样密度的性质，不是候选的性质；基线取错，循环就会把几何记成检测收益。这与第 28 轮"62% 的缺口是几何"是同一件事的两面：那一轮证明**差距**可以由几何解释，这一轮证明**收益**也可以。

**边界 69：在预算上打平的不是多个改进。** 三个邻居在 10% 预算下完全相同（0.427 / 0.080），差别只在 shipped 阈值（0.260 / 0.417 / 0.536），而 shipped 点是另一个决定（第 7 轮的 `operational` 出口）。把它们记成三次接受，账本会虚报改进数量——接受计数从此必须与"当时比较的基线是否同一几何"一起读。

**边界 70：记录里没有本几何的参照基线，那次接受就不是测量点。** 一条旧接受是在"另一几何下的基线"上做出的；`--audit` 能说出"今天的规则会怎么判"，但重判需要那次没做过的测量。所以这类接受只能被排除在种子之外，不能被"重判为接受"——一个无法重建比较的记录，只能当历史，不能当证据。

## 3.39 记录必须能被读回来：复核用错基线，会把几何算成规则改动

上一轮把比较搬到了候选自己的窗口：`run_round` 现在为每个不同的窗口测一条 `baseline@<window>`，
候选只跟同一几何的参照相减。但那条修复只搬了**跑一轮**的路径。`rejudge` 与 `--audit` 走的是
另一条路，它们仍然只读 `payload["baseline"]`——于是同一个候选在复核时被拿 286 的测量去减 200
的基线，几何差被报成了规则改动。

证据是一次"规则没动、判决却动了"的复核：

```
$ python -m brian.cli evolve --rejudge var\evolve\round-20261118T012841Z.json
recorded rules    {"alert_budget": 0.1, "cost_tolerance": 0.05, ... , "recall_tolerance": 0.0}
current rules     {"alert_budget": 0.1, "cost_tolerance": 0.05, ... , "recall_tolerance": 0.0}

candidate                              recorded               re-decided             change
host-rolling-rank-0-85-200@hint286-0-8 reject gain            accept quality         -> accept
host-rolling-rank-0-85-200@hint286-0-75 reject gain           accept quality         -> accept
host-rolling-rank-0-85-200@hint286-0-8-572 accept operational reject recall          -> reject

3 verdict(s) changed. The stored measurements were not re-run; only the rules moved.
```

两个规则集字节相同（指纹 `83abef5480cc`），"only the rules moved" 是一句假话：动的是参照。
`@hint286-0-8` 的"新理由"甚至写着 `recall at 10% false positives 0.297 -> 0.427`——0.427 正是它
自己窗口上的参照值，复核把"和参照打平"读成了"相对旧基线大涨 44%"。

读代码时又发现第二个独立缺陷：`Measurement.from_dict` 从不回读 `alert_budget`、`prevalence`、
`alerts_at_free_cut`、`scored_at_free_cut`。这四个字段写进记录很久了，从来没有读回来过——于是
任何被重新判定的记录都会悄悄丢掉无标签上限。**写出来但读不回来的字段，比没有这个字段更糟：
记录看上去是完整的。**

修法三件：

1. `Measurement.window`：一条测量记录自己带上它被测量时的窗口。`to_dict()`/`from_dict()` 成对，
   `test_every_measurement_field_is_written_out` 用 `dataclasses.fields()` 兜住所有字段，将来再加
   一个字段而忘了序列化，会直接红。
2. `recorded_reference(payload, measurement, baseline)`：有 `window` 就取
   `references[str(window)]`；没有 `window` 时分两种——整份记录里根本没有 `references`（round 31
   之前写下的），按 `baseline` 复算，因为当时就是这么判的；有 `references` 却没有标明这一条用了
   哪一个（round 31 与 round 32 之间写下的记录），**不猜**。
3. 第三种结局：`Rejudge.withheld` 与 `Audit.cross`。判不出来的比较既不是"改判"，也不是"没变"，
   它没有结论；表里单独一列，`--rejudge` 列出被扣下的候选名，并说明为什么扣。

修复前后的账本对比说明了代价有多大：

| 指标 | 修复前（round 31 收盘） | 修复后 |
| --- | --- | --- |
| 会被今天规则改判 | 10 | 7 |
| 改属规则但判决不变 | 22 | 18 |
| 无法重建（不报结论） | — | 23 |
| 重新判定过的测量 | 193 | 170 |

也就是说，那"10 个今天会改判"里有 3 个、22 个改属规则里有 4 个，是复核自己造出来的比较。
修复后 `--rejudge` 新的记录（`round-20261118T013838Z.json`）打印 `0 changed, 0 withheld`——同一份
记录能被读回来，复算得出同一批判决。

三条读法：

- **复核必须问记录"当时用的是哪个参照"**，而不是"现在默认的参照是什么"。参照是那一轮实验条件
  的一部分，和几何、采样密度一样，不属于可选项。
- **写了但从不读的字段比缺字段更危险**，因为缺字段会报错，而它只是安静地丢信息。
- **"无法重建"是第三种结局**。把它并入"没变"会让一条读不回来的记录看起来被验证过——这正是
  这一轮要消灭的那类句子。

**边界 71：复核必须使用当轮使用的同一个参照，否则它报告的是自己的比较，并把几何说成规则改动。**

**边界 72：一次测量必须带上它被测量时的几何；参照存在但没有标明用在哪一次测量上，等于没有参照。**


## 3.40 退出码是一种断言：0 不能表示"我读得了多少就查了多少"

上一轮给了 `--audit` 第三种结局（无法重建），但它只活在表尾一段话里；`--strict` 仍然是

```python
return 1 if (args.strict and result.moved) else 0
```

于是一份"23 条比较读不回来"的账本，在 CI 里是**绿的**。这正是这一轮要消灭的那类断言：
"没有判决改变"被印在一串没人看过的试验上面。检查器读回来多少就查多少，剩下的沉默——而沉默在
一个退出码里等于通过。

修法两件：

1. **campaign 上的第三列**。`audit` 行现在读：
   `7 verdict(s) would be decided differently today, 18 relabelled, 23 not reconstructable (all from
   records written before the geometry was recorded)`。0 个移动、0 个改属规则、23 个读不回来，和
   "35 轮全部通过"不是同一句话，现在表头就把它们分开。
2. **`--strict` 三种退出码**。0 = 记录仍然支持今天的判决；1 = 有判决会被改判；2 = 账本有一部分
   读不回来，检查没有完成。2 优先于 1：一个"改判"是答案，而"读不回来"是没有答案——只报告自己
   能读完的那部分，就是这一轮要堵的那个洞。

但这里有个更难的判断。23 条读不回来的比较全部来自 round 31 与 round 32 之间写下的记录：参照存在，
但每条试验没标明用了哪一个。那是**当时还没装上那个字段**的结果，不是这个构建弄丢了什么。把这种
历史欠账算进 `--strict`，红叉会永远挂着，直到有人去重写本不该被重写的历史——而一个永远红的检查，
最后一定会被关掉。

所以 `Audit.cross` 拆成两种债，用 `measurement.window` 区分：

- `kind="legacy"`：没有 `window`，记录写在字段存在之前。它读不回来，但它没有错；重跑那一轮会写下
  一份读得回来的记录。表尾说明，不判失败。
- `kind="missing"`：有 `window` 却没有对应的参照。这是**当前构建的缺陷**——它答应了记录几何，却
  没有把参照接上。`--strict` 退出 2。

今天实跑的账本：

```
$ python -m brian.cli evolve --campaign
audit             7 verdict(s) would be decided differently today, 18 relabelled, 23 not reconstructable
                  (all from records written before the geometry was recorded)

$ python -m brian.cli evolve --audit --strict
23 comparison(s) predate the geometry field and cannot be replayed; re-running those rounds writes
records that can be.
They do not fail the check (their rules still move the exit code), but --strict has not verified them.
exit 1
```

注意最后一行：`--strict` 在这里退出 **1**，不是因为那 23 条，而是因为确有 7 个判决会被改判。检查
没有假装那 23 条被验证过——它把它们说出来了，然后让真正的问题决定退出码。

三条读法：

- **退出码是一种断言**，而 0 的意思是"检查跑完了并且没问题"。只跑完一部分就说 0，是把沉默当成
  通过——这比不检查更糟，因为它留下了一条绿色的记录。
- **"读不回来"要分成历史与缺陷**。历史是记录写在字段之前，缺陷是构建明知该写却没写；把两者算在
  同一个计数器里，等于让历史去关掉检查。
- **表头比表尾重要**。第三种结局放在脚注里，读者读到的是"Why this is not a claim"，而不是
  "23 个没有答案的比较"。

**边界 73：检查必须区分"记录早于字段"与"记录丢了字段"；把历史欠账算进闸门，闸门最后一定会被关掉。**

**边界 74：退出码 0 只能表示检查跑在全部记录上并且通过；跑在它读得懂的记录上，不算通过。**


## 3.41 不再回答的问题要说出来：退役一份记录，而不是改写它

上一轮结束时，campaign 上还有一行：

```
audit             7 verdict(s) would be decided differently today, 18 relabelled, 23 not reconstructable
                  (all from records written before the geometry was recorded)
```

那 23 条比较全部来自两份记录：`round-20261118T012841Z.json`（8 条）和 `round-20261118T013236Z.json`
（15 条）。它们都写在 round 31（有 `references`）和 round 32（有 `window`）之间，因此存了参照，却没说
每条试验用的是哪一个。三条路里只有一条是对的：

- **回填**：给旧记录补一个 `references["286"]`。这条路被边界 71 直接否掉——复核必须用它当时用的那个
  参照，补一个等于替那场试验发明一次它没做过的比较。
- **删除**：`--campaign` 的每一个数字都来自这些记录，删掉就是删掉"这个循环测量过什么"。
- **退役**：记录一个字节都不改，只把"机制是否还要为它负责"这件事本身写下来。

所以 `brian evolve --retire <round> --re-measured-as <round>` 做的事是：在 `ledger.jsonl` 的对应行上加
`retired: {at, re_measured_as, overlap}`。`history()` 默认跳过退役记录（`--audit`、种子、campaign 的
轮次与试验计数都跟着跳过），`--history` 仍然列出它们，并标注 `[retired, re-measured as …]`。

退役之前先重跑一次同一条流，得到一份可读的记录：

```
$ python -m brian.cli evolve --stream-file data\smd\stream-check.json
stream            {"anomalous": 192, "cadence_s": 2100.0, "events": 6775, "scored": 1235, …}
in force          v020 promised recall 0.507 at the 10% budget
on this stream    recall 0.297  unmeasurable
baseline                0.297   0.092     3356     11861     100% -
baseline@286            0.427   0.080     3356      8170     100% reference
host-rolling-rank-0-85-200@hint286    0.427   0.080     3356      8170     100% reject
events-10               0.297   0.092     2596      9174     100% accept
events-1                0.297   0.092     1911      6753     100% accept
events-3                0.297   0.092     2063      7291     100% accept
events-2                0.297   0.092     1987      7022     100% accept
events-6                0.297   0.092     2292      8098     100% accept
recorded round var\evolve\round-20261118T015111Z.json
```

然后退役那两份旧记录。**退役不是回答**，所以退役时会把继任者读一遍，数一数它重复了旧记录里的几条试验：

```
$ python -m brian.cli evolve --retire round-20261118T012841Z.json --re-measured-as round-20261118T015111Z.json
retired           round-20261118T012841Z.json  (re-measured as round-20261118T015111Z.json)
                  1 of its 8 trial(s) appear in the successor: host-rolling-rank-0-85-200@hint286
                  the other 7 are not repeated there, so this retirement does not claim they were answered

$ python -m brian.cli evolve --retire round-20261118T013236Z.json --re-measured-as round-20261118T015111Z.json
retired           round-20261118T013236Z.json  (re-measured as round-20261118T015111Z.json)
                  1 of its 15 trial(s) appear in the successor: host-rolling-rank-0-85-200@hint286
                  the other 14 are not repeated there, so this retirement does not claim they were answered
```

一句"重测过了"如果只看文件，是可以靠一条共同的候选把整份记录洗白的：两份旧记录和继任者恰好都测了
`@hint286`，但 013236Z 的另外 14 条（`events-8`、`host-features-3`、`host-scoring-deviation`……）没有人
再测过。它们是被放弃的问题，不是被回答的问题，`overlap` 就是把这句话写进记录里。

退役之后：

```
$ python -m brian.cli evolve --audit --strict
recorded rounds   41  (178 candidate measurements re-decided, no re-run)
retired           2 round(s) were re-measured and are not re-decided; their records stay as written
…
7 verdict(s) would be decided differently today; 18 more kept their verdict but changed rule.
exit=1
```

退出码仍然是 1，但原因变了：上一轮它因为"7 条改判"退 1、同时又挂着一笔永远还不上的 23 条债；现在它
只为那 7 条退 1，`not reconstructable` 从报告里消失，因为**这个问题已经不归这个检查回答**。

同一轮把 audit 拆到每个域。此前它是全账本一个数，而页面上其他每一个数字都是按域的：

```
$ python -m brian.cli evolve --campaign
rounds            41  (178 candidate measurements, 68 accepted, 110 rejected)
audit             7 verdict(s) would be decided differently today, 18 relabelled
retired           2 round(s) were re-measured; their records stay, the checks skip them
SMD                    24    128     48 20   …
  audit             6 moved, 17 relabelled
RCAEval RE1-OB          7     25      6 19   …
  audit             1 moved, 1 relabelled
demo                    9     23     13 -    …
```

7 条里 6 条属于 SMD，RCAEval 只欠 1 条。一个账本级的"7"会让两个域的读者都以为债是自己的。

还有一处是同一个坑的另一半：`previously_rejected()` 原本也走 `history()`，于是**退役会让循环忘掉 23 条
拒绝**——正好是这个函数存在的理由（科学失忆）。修法是让它读退役记录：退役是关于"检查是否还要回答"的
说法，不是关于"循环学到了什么"的说法。新测试
`test_retiring_a_refusal_does_not_unlearn_it` 把这一点钉住。

**边界 75：退役必须指明继任者，并说明继任者重复了旧记录里的哪几条试验。"重测过了"是关于候选的说法，不是
关于文件的说法——一条共同的候选不足以代替其余七条。**

**边界 76：一个检查可以停止提问，但报告必须说明那个问题是被放下了，而不是被回答了。`not reconstructable`
消失的唯一理由是它不再属于这个闸门，而不是因为它被修好了。**


## 3.42 一条命令，两份记录：批量退役与"谁还在替这个拒绝负责"

上一轮结束时，退役是这样用的：

```
$ python -m brian.cli evolve --retire round-20261118T012841Z.json --re-measured-as round-20261118T015111Z.json
$ python -m brian.cli evolve --retire round-20261118T013236Z.json --re-measured-as round-20261118T015111Z.json
```

两条记录、一个继任者、同一条命令写两遍。抄第二遍的时候最容易错的是继任者的名字，而错了之后没有人会报错
——退役是写进账本的，`--audit` 不会再质疑它。所以 `--retire` 现在可重复，也可以逗号分隔：

```
$ python -m brian.cli evolve --retire round-20261118T012841Z.json,round-20261118T013236Z.json \
      --re-measured-as round-20261118T015111Z.json
retired           round-20261118T012841Z.json  (re-measured as round-20261118T015111Z.json)
                  1 of its 8 trial(s) appear in the successor: host-rolling-rank-0-85-200@hint286
                  the other 7 are not repeated there, so this retirement does not claim they were answered
retired           round-20261118T013236Z.json  (re-measured as round-20261118T015111Z.json)
                  1 of its 15 trial(s) appear in the successor: host-rolling-rank-0-85-200@hint286
                  the other 14 are not repeated there, so this retirement does not claim they were answered
retired           2 of 2 round(s), each naming round-20261118T015111Z.json
```

**批量的合并只在命令层，不在这项判断里**：两条记录各自和继任者比一遍，各自报自己的重叠。它们恰好共享同一个
继任者，但继任者只重复了 1 条候选，所以两份记录都只主张 1 条。一个名字写错时整条命令退 2，并且只打印失败的
那一条：

```
$ python -m brian.cli evolve --retire round-20261118T012841Z.json,round-nope.json
retired           round-20261118T012841Z.json  (re-measured as round-20261118T015111Z.json)
cannot retire round-nope.json: no recorded round matches round-nope.json
retired           1 of 2 round(s)
exit=2
```

## 页面上于是出现两个新数字

```
$ python -m brian.cli evolve --campaign
rounds            41  (178 candidate measurements, 68 accepted, 110 rejected)
retired           2 round(s) were re-measured; their records stay, the checks skip them
refusals          35 candidate(s) are still held as rejected, 14 of them on retired record(s)
…
SMD                    24    128     48 20        recall 0.507 at the 10% budget, 3358 tokens/report
  audit             6 moved, 17 relabelled
  retired           2 round(s): re-measured as round-20261118T015111Z.json
```

第二个数字是这一轮真正的发现：循环"已经定下来"的 35 条拒绝里，**14 条来自退役记录**——那两轮没有再被
复核过。上一轮把它们从 `--audit` 里拿掉是对的（那个检查不欠它们答案），但 `previously_rejected()` 仍然
读它们也是对的（循环确实学到过这些拒绝）。错的是页面：它把 35 条并成一列 `rejected by`，读起来像"这 35 条
是刚判过的"。现在它分开说：

- `refusals N …` 是下一轮实际会跳过的候选数（`previously_rejected()` 的返回长度）；
- `, M of them on retired record(s)` 是其中没有人再复核过的条数。

为此 `previously_rejected()` 被拆成两层：新的 `rejection_provenance()` 返回每条候选的
`{accepted, reason, source, round, retired, re_measured_as}`，"谁判的"和"判了什么"放在一起；
`previously_rejected()` 变成它的一个薄投影（`on <source>: <reason>`），输出一字未变，旧调用方不受影响。

**边界 77：批量退役是一条命令，不是一次判断。每条记录都要和继任者单独比一遍并报出自己的重叠；共享同一个
继任者不等于两份记录都被回答过。**

**边界 78：一份"已定"清单必须说明其中有多少条没有人再复核。把继承来的拒绝和刚判过的拒绝并成一列，读起来
就是一次不存在的验证。**


### 3.43 拒绝是一句关于数据和采样的话：四种拒绝，只有一种是节省

第 35 轮把"35 条仍被判为拒绝、其中 14 条来自已退役的记录"报了出来。这个数字本身还是漏掉了一件事：
**四条里有三条是"应该再测一次"的理由**，只有一条真正省下一次测量。

- 同一数据流、同一采样密度、未被退役 —— 这条拒绝已经测过，再测是重复付费；
- 拒绝来自**另一条数据流** —— 该候选在本地从未被测过，这一次测量才是第一次；
- 拒绝来自**已退役的记录** —— 没人复核过那个判决，重新打开它正是测量的意义（边界 76：问题是被丢掉的，不是被回答的）；
- 拒绝是在**另一种采样密度**下做出的 —— 那些窗口不描述这里的流量（边界 72），旧拒绝不适用。

于是在 `src/brian/evolve.py` 里加了 `refusal_plan(provenance, source=..., shape=...)`，把仍然成立的拒绝
拆成 `skip` / `elsewhere` / `inherited` / `reopened` 四组；`previously_rejected` 也接受 `source` 参数，
因为"一个候选在合成数据流上被拒"和"它在 SMD 上被拒"不是同一个陈述。实测（`data/smd/stream-check.json`）：

```
  skip        14 candidate(s)  events-18, events-32, host-rolling-rank-0-85-100, ...
              refused on this stream at this sampling: not measured again
  inherited   14 candidate(s)  analyze-pages-only, anomaly-p-40, confidence-auto-85, ...
              refused by a retired record nobody re-checked: re-opened
  reopened     7 candidate(s)  host-rolling-rank-0-75, host-rolling-rank-0-8, ...
              refused at a different sampling: re-measured on these windows
```

**这一轮省下了几次测量？零次。** 干跑打印 `would skip 0 of 8 proposal(s) refused on this stream at this
sampling`，条目化运行（`--catalogue`，14 个候选）里也没有一个落在 `skip` 组：`skip` 里的名字是机制现在
不会再生成的那些（它们的种子早被拒了，邻居链断了）。所以这一轮交付的是一个**守卫**，不是一次节省——
而且它必须把"没有命中"也打印出来，否则读者无法区分"守卫查过了、没命中"和"守卫根本没接上"（边界 79）。

**第二个发现由按域拆分带出来**：`--campaign` 页面上 `SMD  refusals  35 held, 5 inherited`，而 demo 和
RCAEval 两行的 `refusals` 行消失了——因为按今天的规则重判之后，它们记录的拒绝全部翻成了接受。
同一页面上另一行 `rejected by  gain x10` 仍然是"写下来时的判决"，两个数不是一回事。于是给后一行加了
`(as recorded)` 后缀（边界 80）：一个页面上的两个"拒绝"列，如果数的是不同的东西，就必须各自说明数的是什么。
这正是第 35 轮"谁还在替这个拒绝负责"的下一层：**拒绝的归属不仅是"哪条流"，还是"哪一天"**。

`--candidates` 命名的候选不受守卫影响：显式点名是一条指令，不是一次提案，悄悄跳过它会让这个开关对唯一
被要求的事情保持沉默。实测 `--candidates events-18` 报 `unknown candidates: events-18`（它不在目录里，
是生成出来的邻居），所以这条路径目前只有单元测试覆盖。

**边界 79：一个在当前账本上从不命中的节省，必须被报成守卫而不是节省。** 否则"这一轮省了多少"就成了
无法证伪的句子——省下的次数没法与"根本没接上"区分开。

**边界 80：同一页面上数不同东西的两列，必须各自说明数的是什么。** `rejected by`（写下来时的判决）与
`refusals`（今天仍然成立的拒绝）在一次重判之后会不一致；不标注就等于让读者以为自己在看同一个数。


### 3.44 提案者不该重新问一遍：过滤器要放在生成器里

第 36 轮把"这一轮可以跳过哪些候选"算清楚了：`refusal_plan` 把仍然成立的拒绝分成四组，`_skip_refused`
再从 `selected` 里把 `skip` 那组摘掉。但它是在 `propose` **之后**摘的，而 `propose` 已经把名额花掉了：
提案上限是 8，八条邻居先生成、先排序、先截断，过滤器拿到的是一份已经装好的名单。于是同一轮里能读到两句话，
一句说账本上 14 个问题已经有答案，一句说"would skip 0 of 8 proposal(s)"——不是两边矛盾，而是过滤器根本没有
可以摘的东西：机制从不会再生成这些名字，因为它们当初被拒的种子已经不在种子里了。

真正的问题在名额上。一个被本数据集拒绝过的候选如果还能占掉一个名额，这一轮就等于用一次测量去换一个已知答案；
过滤器后置时，这件事发生在 `propose` 内部，谁都看不到。第 37 轮把过滤器搬进生成器：

- `Proposal` 多一个 `refused` 字段（`to_dict()` 里叫 `refused_skipped`），四个打印点都会说出这个数；
- `propose(..., refused=[...])` 在**生成邻居时**就跳过这些名字，目录路径（`_from_catalogue`）同样过滤
  ——目录是一串问题，本数据集答过的问题在这里不再开；
- 被拒绝的候选占满了空间时，理由要说清楚："N neighbour(s) were already refused on this stream at this
  sampling and the rest of the space is exhausted"；
- `reshape`（承诺测试）不受这条规则约束：它不是重复提问，是一条记录里承诺的复测；
- cli 把 `refusal_plan` 提前到 `propose` **之前**（正式轮与 `--propose` 干跑都一样），并把
  `refused_skipped` 写进 `proposal_record`，随轮次记录一起落盘。

干跑的前后对比（`data/smd/stream-check.json`，同一份账本）：

```
before  would skip        0 of 8 proposal(s) refused on this stream at this sampling
after   2 candidate(s) were dropped because this stream already refused them at this sampling
        would drop        2 candidate(s) this stream already refused at this sampling
```

被丢掉的是 `host-scoring-deviation-band-0-92` 与 `-0-82`：它们在本采样上已经被拒过一次。

正式轮的同一条证据（`round-20261118T021614Z.json`）：`proposal.refused_skipped = 3`——两条 band 邻居加上被
本采样拒过的 reshape——八条候选里补进了 `host-scoring-deviation-band-0-77`，而它上一轮正好被上限截掉；八条全部
接受（`events-10` −22.7%、`events-1` −43.1%、`events-3` −38.5%、`events-2` −40.8%、`events-6` −31.7%，
三条 band 走 `cost`）。同一份账本上一轮读到的是 `not measured 1 candidate(s) the proposal includes`，这一轮
这一行消失了：没有需要事后摘掉的候选。

三句读法：

1. **省下来的是名额，不是测量次数。** 提案仍然有 8 条，只是那两条不再进来，空出的两个名额被 `-0-97` 与
   `-0-77` 补上——它们本来被上限截掉了。所以这一轮的收益是"八次测量全都问新问题"，不是"少测两次"；把名额
   省下来当成测量次数来报，是把上限当成了花销（边界 81）。
2. **数字要落在产物里。** 第 36 轮的"跳过 14 个"只出现在终端里，读轮次文件的人看不到，于是"这个循环省了多少"
   只能靠读日志回答。现在 `refused_skipped` 写在 `proposal` 记录里，账本自己就能回答（边界 82）。
3. **过滤器搬进生成器后，`--catalogue` 与 `--candidates` 的旧规则仍然成立。** 目录项按同一条规则过滤；
   调用者点名的候选照旧测量——点名是指令，不是提案（第 36 轮就定下的区分）。

4. **承诺测试也不该重复问。** 第一版把 `reshape` 豁免了，理由是"它不是重复提问，是承诺的复测"。但同一份账本
   已经在本采样上回答过这条承诺（0.427 对 0.507，拒绝），于是这一轮的提案照样把名额花在它身上，日志里出现
   `not measured 1 candidate(s) the proposal includes, already refused on this stream:
   host-rolling-rank-0-85-200@hint286`——过滤器在事后摘掉它，等于承认生成器刚刚浪费了一个名额。豁免的应当是
   "用哪个裁判"（`promise` 规则），不是"要不要再测一次"；修正后 `reshape` 与其它候选一样被生成器过滤、计数，
   腾出的名额给了 `-0-77`。

**边界 81：提案有上限时，被跳过的候选换来的是一个不同的问题，不是一次更便宜的运行。** 说"这一轮省了 N 次
测量"之前，先看 `len(candidates)` 有没有变。

**边界 82：一个机制省下的东西必须写进它改动的产物，否则它只是一个 print。** 终端会滚走，轮次记录不会。

### 3.45 省下来的名额去了哪里：一次替换，或者一次沉默

round 37 把 `refused_skipped` 写进了每一轮的记录，也在那一轮打印出来。这一轮补的是它留下的两个问题：
这个数没有任何人累加；以及"丢了 N 条候选"读起来像"省了 N 次测量"。

后者在有名额上限的提案里是错的。生成器每轮最多提 8 条，被拒绝的候选空出一个名额，名额会被后面的
候选填上——所以省下来的不是一个测量，而是一个名额。round 37 的表格里 `host-scoring-deviation-band-0-77`
正是这样进来的：上一轮它被上限切掉，这一轮两条同族候选被拒绝后它进了名单。

**第一处：替换要有名字。** `propose` 现在并行记下一条**没有**过滤的生成顺序（`without_refusals`，
与过滤后的顺序共用同一份 `known` 去重表），然后比较两者被上限截断后的前 `cap` 条：

```
selected       = generated[:cap]
baseline_order = ([reshaped] if reshaped else []) + without_refusals
substitutes    = [c for c in selected if c.name not in {b.name for b in baseline_order[:cap]}]
```

`Proposal.substitutes` 进 `to_dict()`（键 `substitutes`），两个打印器各一行：

```
                1 slot(s) went to candidate(s) the cap would have cut: events-2
1 slot(s) went to candidate(s) the cap would otherwise have cut: events-2
```

在真实账本上，`data/smd/stream-check.json` 的 dry run 丢掉两条（`host-scoring-deviation-band-0-92/-0-82`），
替换列表是**空的**：

```
2 candidate(s) were dropped because this stream already refused them at this sampling

candidate                      change
events-5                       llm.max_context_events = 5
...
```

这是诚实的那种答案：两条被拒绝的候选在**没有**过滤的顺序里本来就排在第 9、第 10 位，上限是 8，
它们本来也不会被测量。所以这一轮丢掉的不是上限内的名额，而是生成器原本多余的两条。一个非空的替换
列表只在名额确实被填上时出现——用了两个已测量的种子和 `max_proposals=2` 的构造用例：

```
unfiltered ['events-16', 'events-9', 'events-2', 'events-5']
selected   ['events-9', 'events-2']
substitutes ['events-2']
```

被拒绝的 `events-16` 空出的名额给了 `events-2`；没有它，这一轮只测 `events-9`。

**第二处：节省要累加。** `Campaign.skipped`（`to_dict()` 键 `measurements_not_repeated`）与
`CampaignRow.skipped` 从每一轮记录的 `proposal.refused_skipped` 累加，页面上一行表头、一行域：

```
refusals          35 candidate(s) are still held as rejected, 5 of them on retired record(s)
saving            6 candidate measurement(s) the mechanism never proposed: a stream had already refused them

SMD                    27    157     68 20        recall 0.507 at the 10% budget, 3358 tokens/report
  refusals          35 held, 5 inherited from a retired record
  saving            6 candidate measurement(s) not repeated
```

三条读法：

1. **有名额上限时，"省了"只能是替换或明文否认。** 候选被丢掉之后名额会被别人占走，所以"省了一次测量"
   这个说法在上限内是假的；唯一可核对的说法是"这一格给了谁"，或者"没有任何一格易主"。前者有名字，
   后者说明被丢掉的候选本来就排在名额之外。
2. **一次性的打印不是账。** 只出现在某一轮日志里的节省，和从未发生的节省无法区分：下一轮从同一本账出发，
   照原来的方式提案。所以这个数必须累加到读者会看的那张表上。
3. **空的替换列表本身是信息。** 它说这一轮丢掉的候选不会进入测量，也就是说 round 37 的收益在"生成"，
   不在"测量预算"——两个说法在报表上长得一样，在工程上完全不同。

**边界 83：一个被拒绝的候选在有名额上限的提案里省下的不是一个测量，而是一个名额；名额会被别人占走，
所以"省了"必须报成替换（有名字）或报成"没有替换"（名额本来就不属于它）。**

**边界 84：一次性的打印不是账；节省必须累加到读者会看的那张表上，否则它与"从未发生"不可区分。**


## 3.46 池化数字是一次除法：一个 +5% 的增益可以只属于一台机器

`recall_at_budget` 不是某一台机器的数字。它是把每条 recording 的 incident 合起来、命中数除一次，
所以权重是各自的 incident 数。SMD 的 check 流上，在 force 的 v020 读作 **0.297**，拆开是
**0.491**（machine-1-6）和 **0.037**（machine-2-2）——第二台的 incident 更多，pooled 的主角就是它。
部署要同时跑在两台上，所以"pooled 涨了"这句话本身不构成检测增益。

这一轮先证明它可以骗人，再把规则装上。探针（`var/probe_split39.py`）量了同一个流上的 baseline 与
目录里的候选，分机器列 recall@b：

```
candidate              pooled   smd-machine-1-6   smd-machine-2-2
baseline                0.297   0.491             0.037
host-features-3         0.312   0.382             0.220
```

`host-features-3` 是目录里真实的一条：把 8 个特征砍到 3 个。它 pooled **+5.0%**，正是质量条要的幅度；
代价是 machine-1-6 从 0.491 掉到 0.382，换来 machine-2-2 从 0.037 到 0.220。按 pool 判，它该被接受；
按"两台都别变差"判，它是一台机器的改进加另一台机器的退步。

规则装在两处。`Measurement` 现在记录 `recall_by_recording` / `fpr_by_recording`（`measure()` 从
`operating_point_by_recording()` 的 `by_recording` 填入，`to_dict()/from_dict()` 往返），
`decide()` 在 pooled 质量增益成立之后、两条质量接受出口之前调用 `_replication_loss()`：取
`baseline` 与 `trial` 共有的 recording，少于两个不判，任何一台掉超过 `min_quality_gain` 就返回

```
recall at the 10% budget rises 0.297 -> 0.312 pooled, but smd-machine-1-6 falls 0.491 -> 0.382:
the gain belongs to the recordings that rose, so this is not a replicated one
```

判成第四条规则 `replicate`。阈值刻意取与质量增益同一个 `min_quality_gain`：比这更小的抖动本来也不
足以让候选被接受，规则不能在拒绝时用更严的尺子。

实测一轮（`--catalogue`，`data/smd/stream-check.json`，记录
`var/evolve/round-20261118T022739Z.json`）：`host-features-3` 量到 0.312，**reject**，理由是上面那句；
同轮 `events-8`、`events-4`、`host-scoring-deviation`、`deviation-bar-free` 四条照样接受，分机器数字
完全没动（0.491 / 0.037）。表后现在固定打印分机器行：

```
  recall@b by recording:
  baseline             smd-machine-1-6 0.491 -> 0.491  smd-machine-2-2 0.037 -> 0.037
  host-features-3      smd-machine-1-6 0.491 -> 0.382  smd-machine-2-2 0.037 -> 0.220
```

三点读法。**第一**，pooled 是一次除法，不是一次测量：读者看表时看到的是加权平均，权重藏在 incident
数里，所以"哪一台动了"必须单独打印，否则一条 +5% 的接受可以是一台机器的故事。**第二**，复现要求是
"不能更糟"，不是"必须更好"：规则只在某台掉过阈值时拒绝，一台涨一台不动仍然可以接受——否则异构域上
任何改动都会被拒，而阈值本来就只在其中一台上校准过。**第三**，历史记录追不回来：`--audit` 对旧记录
仍是 7 moved / 18 relabelled，没有因为新规则多翻一条，因为那些记录没有分机器字段，`_replication_loss`
在它们身上无处下手。这不是漏洞而是教训的复述——数字没写进 artefact，就没有第二次机会被查（边界 86）。

**边界 85：池化增益必须在每条 recording 上复现，否则它就是一条 recording 的改进，而部署跑在所有
recording 上。** "整体变好"是一个除法结果，不是一条可以被部署的陈述。

**边界 86：循环对 recording 取平均的任何数字，都必须按 recording 打印。** 否则读者看得见平均、看不见
权重，也就无法判断是哪一台在移动。


## 3.47 容忍度是个假设：把拒绝的数字写进记录

3.46 的拒绝是一句话，而判它用的 5% 硬编码在 `decide()` 里。记录里只有"池化涨了、machine-1-6 掉了"，
没有"掉了多少、对哪个容忍度"。这有两个后果：后来的人不同意这条拒绝时，只能重测一遍；`--audit` 也
无法说出"这条拒绝有多依赖那个假设"。

这一轮把它拆开。`EvolveRules` 多一个字段：

```python
replication_tolerance: Optional[float] = None    # None -> replication_bar 回落到 min_quality_gain
```

默认回落到质量门槛，不是随手写的：拒绝不能比接受更严——如果一条 +5% 的池化增益值得量，那么量它的
容忍度就该是同一个数。它进 `to_dict()`，也就进 `rules_fingerprint`，所以改这个数会让"当时按什么规则
判的"在账本上可见（下面 `--audit` 的两行就是这样来的）。

`replication_loss()` 现在返回数字而不是句子：

```
{"recording": "smd-machine-1-6", "before": 0.4911, "after": 0.3822, "drop": 0.1089,
 "tolerance": 0.05, "pooled_before": 0.2971, "pooled_after": 0.312, "budget": 0.1}
```

句子由 `_replication_sentence()` 从它生成，`Verdict` 多一个 `detail` 字段把这份数字写进 round 记录，
`rejudge()` 读回来——否则就是 3.38 的教训重演一次：字段写了没人读，记录看起来完整，实际上不可复核。

`--replication-tolerance` 同时作用于 `--rejudge`、`--audit`、`--campaign`、`--propose` 和整轮。为此顺带
修了一个循环里的复制粘贴：`rules = EvolveRules(min_gain=args.min_gain)` 原先有**六处**，加一个字段就要
改六次，漏一处就会出现"某个模式下这个 flag 静默无效"。现在只有 `_rules_from_args(args)` 一处。

实测三条。默认规则重放 3.46 那一轮，逐位复现：

```
host-features-3        reject replicate       reject replicate       -
no verdict changed: today's rules agree with the recorded round.
```

放宽容忍度到 0.20，同一条测量、同一份记录，判决改变：

```
host-features-3        reject replicate       accept cost            -> accept

host-features-3        was rejected: recall at the 10% budget rises 0.297 -> 0.312 pooled, but
                                smd-machine-1-6 falls 0.491 -> 0.382: the gain belongs to
                                the recordings that rose, so this is not a replicated one
                       now  accepted: 3356 -> 2908 tokens/report (-13.4%)
                       the refusal used tolerance 0.050 against 0.200 in today's rules

1 verdict(s) changed. The stored measurements were not re-run; only the rules moved.
```

最后一行是新的：只写"判决变了"会让读者以为测量变了，实际变的只是一个假设，所以把两边用的容忍度并排
写出来。（这句只在记录带 `detail` 时出现；3.46 之前的记录没有这个字段，无从比较，也就不写——这正是
"一句话不能被重新裁决"的证据。）

第三条是账本层面的：`--audit` 在默认规则下 `7 verdict(s) would be decided differently today`、`2
recorded round(s) carry a different ruleset fingerprint`；把容忍度放到 0.20 后变成 **9** 与 **4**。多出来的
两条拒绝依赖的正是那个默认值，而指纹那列说明它们当时是按另一套规则记的。

两点读法。**第一**，一条拒绝记录的是判断，不是事实：把它拆成"数字 + 假设"，后来的人可以不同意判断而
不必重测，而"不同意"必须表现为规则改动、在指纹和 `--audit` 里说话，不能表现为一条新的测量。**第二**，
默认值也是有方向的：容忍度取"等于质量门槛"意味着拒绝与接受用同一把尺子；把它调松是"我接受这台机器
掉这么多"，调紧则是"任何一台都不许掉"，两者都是运维判断，不是数据能回答的问题。

**边界 87：拒绝必须记下产生它的数字——哪条 recording、掉多少、对哪个容忍度。** 只写句子的拒绝，
在记录里看起来像事实，实际上不可复核。

**边界 88：容忍度是假设；把它与拒绝一起存下来，后来者不需要重测就能不同意，而这种不同意是规则改动，
不是测量。** 反过来也成立：改了容忍度就必须让指纹和 `--audit` 说出来，否则账本会把两次不同的判断
读成同一个。


## 3.48 一次拒绝有多依赖那个假设：按域做的敏感度表

3.47 把容忍度写进了记录，但账本里还是只有一个数：`2 candidate(s) are refused for replicate today`。
读者看不出这两条拒绝是"标签结论"还是"容忍度结论"。这一轮把它算出来——不重测，只把存下来的测量在几个
不同的 bar 下重判一遍：

```
$ brian evolve --sensitivity
current rules     bddd4506b43c
bar in force      0.050
refused           2 candidate(s) are refused for `replicate` today
with numbers      1 of them record the loss and the bar, so a reader can re-decide them

domain                  refused  with nums    0.050    0.100    0.200
---------------------------------------------------------------------
SMD                           2          1        0        0        2
```

四点读法。**第一列是控制组**：它就是当下在用的 bar，所以必须是 0——一个 bar 不能接受它刚刚拒绝的东西。
这一列非零就说明表算错了，而不是说明数据有趣。**SMD 要到 0.200 才翻转**：记下来的损失是 0.1089，四倍于
当前 bar，所以这两条拒绝不是"掉了一点点"，是真的在说标签。把 ladder 调到 0.110，两条立刻翻转——边界
因此可以钉在 0.109 上，而不是含糊地说"大概两倍"。**`with nums` 是覆盖率**：两条里只有一条带 3.47 才有的
`detail`，另一条是 round 40 之前写的，读者能重判它、但记录里没有它自己的数字。**没有任何东西被重新测量**：
这不产生新证据，只把已有证据换个假设重读。

反向也成立。把 bar 直接放到 0.20 再问，答案变成"没什么可问的"：

```
$ brian evolve --sensitivity --replication-tolerance 0.20
bar in force      0.200
refused           0 candidate(s) are refused for `replicate` today

no surviving round refuses a candidate for `replicate`, so there is nothing to test.
```

这不是"敏感度消失了"，而是"这条拒绝在那个假设下不存在"。

为什么不做成 `--campaign` 的一列？战役页的契约是"每个数字都是某一轮真的测出来的"，而敏感度表是**假设
下的重读**——它属于诊断，不属于账本。`--audit` 之所以能在那一页，是因为它回答"今天的规则会怎么判"，
那是当下真实在用的规则；敏感度表问的是"如果规则换成 X"，那个 X 从未生效过。两者放同一页会让读者分不清
哪一栏是事实。

**边界 89：把存下来的测量在假设的规则下重判，得到的不是新测量。** 敏感度表能说"这条拒绝依赖哪个 bar"，
不能说"哪个 bar 是对的"；把它印成测量结论就是把假设当事实。

**边界 90：只能重判一部分账本的表，必须印出它的覆盖率。** 只有带 per-recording 数字的记录能被这样重判，
不带的那些（round 39 之前）根本不会触发这条规则。表若不写 `not replayable` / `with nums`，就会把它们当成
"没有拒绝"，从而高估自己的域。


## 3.49 拒绝也是一条记录：把它写成"没发布的版本"

第 41 轮的敏感度表能回答"这次拒绝有多依赖那个 bar"，但它读的是**轮次记录里的 verdict**。于是还有两个问题没人能答：
一个候选被拒绝之后去哪找它？如果 bar 改了，谁来指出"这条拒绝已经不成立了"？verdict 只活在轮次文件里，而轮次文件
按时间写，不按候选写。

这一轮把拒绝写到版本家族旁边：`var/evolve/withheld/w###.json`，`w` 前缀保证它永远不会被当成发布过的 `v###`。一条
真实记录（SMD 上的 `host-features-3`）：

```
{
  "withheld": 1,
  "round": "round-20261118T024405Z.json",
  "stream": {"source": "SMD", "windows": 1355, "scored": 1235, "cadence_s": 2100.0},
  "candidate": {"name": "host-features-3", "changes": [["digest", "host_features", "cpu_r,load_1,mem_free"]]},
  "bar": 0.05,
  "loss": {"recording": "smd-machine-1-6", "before": 0.4909, "after": 0.3818, "drop": 0.1091,
           "tolerance": 0.05, "pooled_before": 0.2969, "pooled_after": 0.3125},
  "measured": {"recall_at_budget": 0.3125, "fpr_at_budget": 0.0968, "tokens_per_report": 2907.7, ...},
  "reason": "recall at the 10% budget rises 0.297 -> 0.312 pooled, but smd-machine-1-6 falls 0.491 -> 0.382: ...",
  "rule": "replicate"
}
```

`brian evolve --withheld` 把它读回来，并且**用记录里的 bar 判**：

```
bar in force      0.050
records           1 refusal(s) recorded
no longer hold    0 of them would not be refused at today's bar

id     candidate                domain                      bar     loss whose gain it was
------------------------------------------------------------------------------------------
w001   host-features-3          SMD                       0.050    0.109 smd-machine-1-6

w001   host-features-3          the loss was 0.109 against a bar of 0.050; today's bar is 0.050, so it still holds
```

把 bar 放宽到 0.20（`--replication-tolerance 0.20`，第 40 轮加的那个参数），同一份数据、一个字都不重测，结论就变了：

```
no longer hold    1 of them would not be refused at today's bar
w001   host-features-3          the loss was 0.109 against a bar of 0.050; today's bar is 0.200, so this objection no longer holds
```

`--campaign` 也认这两条记录。默认 bar 下头部是 `withheld          1 candidate(s) were measured and refused for replication`，
行内是 `  withheld          1 candidate(s) refused for replication`；放宽到 0.20 之后两处都带上
`1 no longer hold at today's bar (0.200)`。**版本页第一次能说出"测过、故意没上"的那一半**，而且说的不是"当时拒绝了"，
而是"按今天的假设还该不该拒绝"。

三点读法：

1. **拒绝是候选的属性，不是轮次的属性。** 轮次文件按时间写，"这个候选为什么没上"要翻遍所有轮次；`w###` 按候选写，
   一条记录就是一句可核对的话："在这个 bar 下，它在这台机器上掉了 0.109"。
2. **判一条拒绝，用它自己的 bar。** bar 存在记录里（第 40 轮），所以复核问的是"当时的判断在当时的假设下是否成立"，
   而不是拿今天的假设改写昨天。两个 bar 并排打印，读者知道分歧落在哪一个假设上。
3. **"不再成立"不等于"该被接受"。** 复核只覆盖 `replicate` 这一条分支：成本、证据、部署底线都没跑，所以表里说的是
   "这条反对意见不成立"，不是"这个候选现在会被接受"。这是第 20 轮那个错误的同类：把上界当成决定。

**边界 91：一次拒绝必须写成一条记录，而不是一句理由。** 理由能读、不能查：它没有数字、没有 bar、没有说是哪台机器，
所以下一个人想有意见就只能重测一遍。拒绝和版本是同一种东西，只是承诺的方向相反。

**边界 92：判一条拒绝，用记录里的那个假设，并把两个假设并排打印。** 用今天的 bar 判一条旧拒绝，是拿新假设改写旧证据；
只打印结论（"不成立"）而不打印两个 bar，是让读者自己猜分歧在哪。复核可以不同意旧假设，但必须先把它说出来。


### 3.50 被跳过的拒绝永远不被检验：让拒绝过期

第 37 轮把拒绝过滤器搬进了生成器：一个在这一流、这个采样上被拒绝过的候选，**不会再被提出**。第 42 轮又把拒绝写成了 `w###` 记录。两件事合起来是一个闭环缺陷：

> 被 `skip` 的候选不会被提出 → 不会被测量 → 它的拒绝永远不会被推翻。

第 36 轮把"拒绝"分成四种，只有一种该省测量，其余三种都是"必须重新测量"的理由。但那三种是**关于数据**的理由（换流、换采样、被退役）。第五种理由是关于**时间**的，而且它自己不会出现：一份拒绝只要曾经成立，就永远成立——除非有人手动 `--candidates` 点名它。这不是信念问题，是记账问题：记录里说"这个候选在 smd-machine-1-6 上会掉 0.109"，而这句话从来没被复测过，读者却把它当作事实。

第 29 轮给承诺做过的处置（一份复核会过期）在这里同样适用，而且更该适用：复测一条拒绝只要一个候选名额，比复测一次部署便宜得多，所以没有理由让它活得比承诺更久。

```
$ brian evolve --propose --stream-file data\smd\stream-check.json
  skip        25 candidate(s)  analyze-pages-only, anomaly-p-40, …, reuse-300
              refused on this stream at this sampling: not measured again
  …
would drop        10 candidate(s) this stream already refused at this sampling

$ brian evolve --propose --stream-file data\smd\stream-check.json --refusal-expiry-days 0.001
  reopened     7 candidate(s)  host-rolling-rank-0-75, …, host-rolling-rank-50-0-95
              refused at a different sampling: re-measured on these windows
  expired     25 candidate(s)  analyze-pages-only, anomaly-p-40, …, reuse-300
              refused here, but longer ago than the expiry: the evidence has aged, so it is measured again
would drop        0 candidate(s) this stream already refused at this sampling
```

默认期限是 `REFUSAL_EXPIRY_DAYS = 14`，和承诺的期限一样长。过期不是"这个拒绝错了"，而是"这个拒绝的证据到期了"：`skip` 因此变成 `expired`，候选重新进入提案，测完再判。`--refusal-expiry-days` 只是把这个假设摊开，好让读者自己试——0.001 天的运行把所有旧的拒绝一次放回提案池。

**写回。** 复测之后，新数字必须落回**拒绝它自己的那条记录**，否则 `w###` 回答"为什么没发布"时引用的是一个早于问题的测量：

```
$ brian evolve --candidates host-features-3 --stream-file data\smd\stream-check.json
…
x host-features-3        recall at the 10% budget rises 0.297 -> 0.312 pooled, but
                         smd-machine-1-6 falls 0.491 -> 0.382: the gain belongs to
                         the recordings that rose, so this is not a replicated one
  re-check  host-features-3  w001: still
recorded round var\evolve\round-20261118T025528Z.json

$ brian evolve --withheld
records           1 refusal(s) recorded
no longer hold    0 of them would not be refused at today's bar
re-measured       1 of them have been measured again since the refusal
…
w001   host-features-3   re-measured 2026-11-18: recall at the 10% budget rises 0.297 -> 0.312 pooled, …
```

三件在这一轮里被运行暴露出来、而不是被读代码想出来的事：

1. **复测会给同一个候选发第二个号。** 第一版写回把"根目录"算成了 `directory.parent`，而 `EVOLVE_DIR` 是两段（`var/evolve`），于是它在 `var/var/evolve/withheld` 里找已有记录，找不到，于是每次复测都铸造一个新的 `w###`。当场看到 `w001` 和 `w002` 并列，都是 `host-features-3`——"为什么没发布"于是有两个答案。修法是显式传根目录，并且**一个候选只有一条拒绝记录**：复测是那条记录的 `checks`，不是新记录。编号数的是被拒绝的候选，不是测量次数。
2. **复测可能根本不测这件事。** 如果新的测量没有逐录音的 recall（旧格式、或只有一个录音），`replication_loss` 返回 `None`，写回的 verdict 会是 `no longer`——那等于用"没人取过的证据"宣布拒绝作废。现在这种情形写 `unknown`，理由直说"这次测量没有说每个录音各自如何，所以它检验不了针对其中一个录音的反对意见"，而 `withheld_staleness` 遇到 `unknown` 回落到记录里的算法，拒绝维持原状。
3. **写回必须发生在 `persist` 里。** 第一版让 CLI 在 `persist` 之后补写，于是脚本跑的一轮不会留下复核记录，只有交互运行会——同一件事两个入口，只有一个记账。现在 `persist` 返回 `{"round", "versions", "withheld", "checks"}`，CLI 只负责把它打印出来（"没人被告知的写回"正是这一轮要修的缺陷）。

**边界 93：一个被 `skip` 的拒绝必须过期，否则机制拒绝检验自己的结论。** 这不是说 14 天是对的：期限是一个假设，`--refusal-expiry-days` 存在的意义就是让这个假设可以被改、被看见。错的是"永远"，因为它把验证成本定成了零。

**边界 94：复测属于它检验的那条记录，而编号数候选、不数测量。** 给同一个候选第二次拒绝编一个新号，会让"有多少个候选被拒绝"变成"拒绝被写了多少次"，而后者随复测次数增长——一个读者用前者做判断，会得到一个随运行次数漂移的数。

### 3.51 复核要站在拒绝成立的地方：自己撞上的两个缺陷

第 43 轮让拒绝过期、并把复测数字写回记录，但复测还得有人发起：计划把 `skip` 的候选挡在生成器外（第 37 轮），
所以一条被跳过的拒绝，除了"某一轮顺手又提到它"，没有别的复测机会。这一轮让 `--verify` 把 `withheld` 当族来查：

```
$ brian evolve --verify --verify-version 20 --verify-withheld \
      --stream-file data\smd\stream-check.json --refusal-expiry-days 0
verifying 1 promotion(s) from smd on SMD
  v020  digest.rolling_window = 200; digest.scoring = auto; gate.rank_probability = 0.85
        unmeasurable: the re-check measured 1235 scored windows against the 828 the claim was made on (1.49x) …
re-measuring 1 refusal(s) recorded on smd against the deployment in force
  w001  host-features-3        still     recall 0.312 at the 10% budget  (bar 0.050)
every re-measured objection still holds: the numbers that produced it are still the numbers today.
```

第一次跑出来的不是这个。第一次跑出的是这个：

```
re-measuring 1 refusal(s) recorded on smd on the stream's own settings
  w001  host-features-3        no longer recall 0.146 at the 10% budget  (bar 0.050)
1 objection(s) no longer hold.
```

**缺陷一：复测量的是另一个配置。** `w001` 是第 42 轮在**在役部署之上**（`scoring=auto`、窗口 200、
`rank_p=0.85`）测出来的：那轮拒绝 `host-features-3` 时，`smd-machine-1-6` 从 0.491 掉到 0.382。而这次复测
拿的是流自己的基础设置，同一个候选在别的配置下切分不同，于是 0.146 让一条拒绝"作废"了 —— 用另一个配置的
数字，清掉一个配置的反对。修法是把在役部署叠到设置上再测，并把量在哪边的句子打出来
（`against the deployment in force` / `on the stream's own settings`）。重跑后是 0.312，与当初那轮一致。

**边界 96：一次复测必须跑在它要检验的那个承诺所属的配置上。另一个配置的数字什么也清不掉。**

**缺陷二：复核多个版本会把补丁叠起来。** 一次不带 `--verify-version` 的 `--verify` 会挑中所有待复核版本
（v001、v004–v010、v012、v014、v015、v020）。它在验完 `v004` 后调了 `apply_promotion`，于是 v004 的
`BRIAN_LLM_MAX_CONTEXT_EVENTS=4` 与 v020 的三个变量并排生效，部署页自己打出了这句话：

```
the variables in force come from 2 versions: v020, v004 … their sum is a configuration nobody measured
```

每个版本都是**相对同一个基线**测出来的补丁；验两个补丁，是回答了两次不同的问题，不是得到了一个配置。修法是
`stacking_note()`：只要这个域里还有别的版本在持变量，就只记录复核结果、不应用，打印
`(verified, not applied: v20 is already in force for smd, and the sum of two patches was never measured)`。
状态手工恢复到只剩 v020（`var/restore_smd_state.py`；顺带发现第一版脚本假设状态里有 `overrides` 键，
把 v004 的变量从 `sources` 删了却留在 `env` 里 —— 一个没有来源的变量比叠两个版本更糟）。

**边界 95：复核可以清掉一条反对，但不能顺带发布这个改动 —— 否则循环会发布没人提出过的东西。**
**边界 97：两个各自通过的补丁不是一个通过的配置；因为"它验过了"就逐个应用，会得到没人量过的组合。**

**那条坏掉的复核记录没有删。** 删掉会抹掉"确实量过什么"，留着会让一条活着的反对被当作已作废。第三种写法是
`withdrawn`：它保留数字、写清为什么不算（"measured on the stream's own settings rather than the deployment in
force … withdrawn in round 44"），并且不把记录从"到期"里拿掉 —— `withheld_due()` 只跳过 `no longer`，
所以一条 `withdrawn` 的拒绝立刻重新到期、会被再测。

`--withheld` 与 `--campaign` 现在也说得出这件事：

```
records 1  no longer hold 0  re-measured 1 of them have been measured again since the refusal
w001  host-features-3  SMD  0.050  0.109  smd-machine-1-6
  re-measured 2026-11-18: recall at the 10% budget rises 0.297 -> 0.312 pooled, but smd-machine-1-6 falls …
withheld 1 candidate(s) were measured and refused for replication (oldest evidence 0 day(s) old)
```

三段读法：一是"复核"和"发布"是两件事，同一个命令里做，就要在两者之间放一句话；二是一条被推翻的复核必须能
被撤回而不是被删除，否则下一次复测会重复同样的错误，而记录看起来干净；三是"到期"要能主动到达，否则把它做成
只读的报表，等于把第 43 轮的过期又变成一句永不触发的机制。

## 3.52 退出码要收自己页面上的账：一条没人复核的拒绝，和一个能跑遍多个域的命令

3.51 之前，`--strict` 只读三样东西：变了多少判词、多少比较无法复现、以及"本构建的缺陷"。而 3.51 之后，
`--campaign` 的页面上多了一行"按域的到期拒绝数"。这一轮的问题是：**页面上的这个数，没有任何退出码读它。**
一份账本里最老的那条拒绝从没被复测过，`--strict` 仍然退出 0 —— 这正是 3.40 修过的失败（"0 只能表示
'每个记录都查过并且通过'"）在一个更年轻的家族里重演。同时，跨域复测要一个域敲一次命令，而"记得拒绝
存在哪里"不该是操作者的工作。

一条命令跑两个域：

```
$ brian evolve --verify --withheld-stream "smd=data\smd\stream-check.json" \
      --withheld-stream "rcaeval-re1-ob=data\rcaeval\stream-holdout.json" --refusal-expiry-days 0.001
---- smd  <-  data\smd\stream-check.json
re-measuring 1 refusal(s) recorded on smd against the deployment in force
  w001  host-features-3        still  (an identical check is already recorded, so this one is not appended)

every re-measured objection still holds: the numbers that produced it are still the numbers today.

---- rcaeval-re1-ob  <-  data\rcaeval\stream-holdout.json
no refusal recorded on rcaeval-re1-ob is older than the 0-day horizon: nothing to re-measure.

2 domain(s) re-checked, 0 objection(s) cleared. A cleared refusal is measured by the next round that
proposes the candidate; this command promotes nothing.
```

第二个域什么也没做，但它**说了**它什么也没做。这不是客套：一个只报告"忙着"的域的命令，会让"没有到期"
和"没看"长得一模一样（边界 100）。同时"一个相同的复核已存在，所以不追加"也是这一轮要证明的性质 ——
复测是幂等的，连跑十次不会把 `w001` 变厚。

账要收：

```
$ brian evolve --campaign --strict --refusal-expiry-days 0.001 ; echo $?
1 refusal(s) are older than the 0-day horizon and were not measured again (oldest evidence 0 day(s) old):
a refusal nobody re-tests is an assertion the loop cannot lose, so --strict exits 1.
pass --verify-withheld to re-measure them.
1
```

三种失败现在各有各的出口：`2` 是本构建的缺陷（记录带着几何却没有参照，或一个域和一个版本混在一个命令里），
`1` 是账没结清（有判词会变，或者有人没被复核），`0` 是"每个记录都查过并且通过"。把"没跑过"折进 `2`
会把一次没做的检查说成一次坏掉的构建；折进 `1` 的"判词变了"又会让读者以为有人不同意 —— 所以它单列一段，
并且点名下一步命令（边界 98、99）。

守卫也各有话说，而不是抛栈：

```
$ brian evolve --verify --withheld-stream "smd=data\smd\stream-check.json" --verify-version 20
--withheld-stream re-measures refusals; --verify-version re-checks a promise. Pass one, not both:
they answer different questions about different records.                                    (exit 2)

$ brian evolve --verify --withheld-stream "data\smd\stream-check.json"
cannot re-measure refusals: --withheld-stream wants domain=stream.json, got 'data\smd\stream-check.json'
```

**跑起来才发现的缺陷：一个静默的过滤器。** 第一版两域跑出来是

```
---- smd  <-  data\smd\stream-check.json
no refusal recorded on smd is older than the 0-day horizon: nothing to re-measure.
```

而 `var/evolve/withheld/w001.json` 就在那里。原因是记录里存的是数据集名 `"SMD"`，命令里敲的是 slug
`"smd"`，字符串直接比较不相等 —— 过滤器静默地什么都没匹配上，命令看起来"查过了，没有"。静默失败比
响亮失败糟，因为它连解释的机会都不给；改成按 slug 比较（`smd` / `SMD` / `Smd` 都指同一个域），并且
给这条规则补了一个测试。顺带修的第二处：复测的覆盖池原来用流的原始设置构，而测量用的是部署 ——
`build_pool` 只固定事件上限、不固定窗口集合，于是"池子"和"配置"来自两个不同的地方。现在池子也用
`recheck` 构。

**这个家族有多小，也要说清楚。** 为了找第二条真实拒绝，这轮在 RCAEval 上跑了一次 `--candidates events-4`
（`var/evolve/round-20261118T034544Z.json`）：基线 `0.857 / 0.023 / 1706 / 1319`，`events-4`
以成本赢被接受（`0.857 / 0.023 / 1310 / 1013`），**没有触发复制性拒绝**。所以账本里仍然只有一条 `w###`
记录，两域演示的诚实说法是"一个复测了，一个确实没有到期"——机制已经能铺开，证据只有一条。

**边界 98：一个门禁必须收自己报告上印出来的账。** 页面上的数字如果没有退出码读它，它就只是装饰；
"最老的证据多旧"既然写进了表，就要有一个退出码因为它而变。

**边界 99：一次没跑的检查，既不是本构建的缺陷，也不是一条变了的判词。** 它要单独一行、挂在退出码 1
下面，折进任何一边都在对读者说假话。

**边界 100：按域跑的命令必须点出"没有到期的域"，不能沉默。** 沉默让"没有到期"和"没看"无法区分，
而这两件事对一条拒绝意味着完全不同的未来。

## 3.53 拒绝要记得自己住在哪个文件里：不猜路径，只拒绝

3.51 之后，`--verify-withheld` 能一次跑多个域，但要操作者写 `--withheld-stream "smd=data\smd\stream-check.json"`。
记录里存的是**数据集名**（`"SMD"`），不是文件。这个差别在本仓库不是学术性的：`SMD` 有两个文件 ——
`data/smd/stream.json` 是 828 个打分窗口、3000 秒一个窗口；`data/smd/stream-check.json` 是 1235 个、
2100 秒。名字不能确定文件，而**认错文件就是换一个采样再测一遍**，那正是第 26 轮（窗口几何被当成回归）
和第 44 轮（在别的配置上复核）各自的缺陷换了个位置重演。所以这一轮做两件事：让记录记住文件，以及
**拒绝从名字推断路径**。

跑起来先看到的是拒绝：

```
$ brian evolve --verify --verify-withheld
1 refusal(s) do not say which stream file they were measured on: w001 (SMD)
pass --withheld-stream for smd=<stream.json>; a source name does not name a file, and guessing one
would re-check the objection on whatever sampling happens to be at that path.
                                                                                       (exit 2)
```

这不是"慢慢会补上"，而是"现在不能替你做这个决定"：两条 `SMD` 流里选哪一条，是一个关于**这次复核要问
什么**的决定，不能由命令悄悄做。

记录补上路径之后，同样的命令不需要任何参数：

```
$ brian evolve --verify --verify-withheld --refusal-expiry-days 0.001
---- smd  <-  data\smd\stream-check.json
re-measuring 1 refusal(s) recorded on smd against the deployment in force
  w001  host-features-3        still  (an identical check is already recorded, so this one is not appended)

every re-measured objection still holds: the numbers that produced it are still the numbers today.

1 domain(s) re-checked, 0 objection(s) cleared. …
```

改动很小：`RoundResult.stream_path`（由调用方写入，因为轮次拿不到 CLI 的参数）、`Withheld.stream_path`、
`persist` 复制过去、`withheld_streams(root)` 把"每个域自己是哪个文件"读回来、`_self_described_pairs()`
把它们变成命令的入参。页面上多一行覆盖度，**只在不满时出现**：

```
stream files      0 of 1 record(s) say which file they were measured on; the rest need --withheld-stream
```

**旧记录怎么办：按几何补，不按名字补。** 唯一的既有记录 `w001` 写于第 42 轮，没有这个字段。于是写了一个
一次性脚本（`var/backfill_path.py`）：拿记录自己的 `stream` 摘要（1355 个窗口 / 1235 个打分窗口）去比对
磁盘上每个候选流文件的摘要，**恰好匹配一个**才写入。输出：

```
w001.json -> data\smd\stream-check.json (windows 1355, scored 1235)
```

这是一次有证据的重建，不是猜测：如果零个或多个文件匹配，脚本什么都不写。补字段的代价就在这里 ——
两种做法都比第三个选项好：留着不补，账本里最老的记录就永远没人能测；按名字猜，测的是另一条采样。

顺带修的第二处：`--verify` 在"没有待复核版本"分支里判断"有没有到期拒绝"用的是原始字符串比较 ——
与 3.52 修掉的过滤缺陷同一处代码的另一个副本。**同一个错误写两遍，就会出现两次**；这次是读代码发现的，
不是跑出来的，也记在这里。

这一轮的现场证据（`var/round46.txt`，记录 `var/evolve/round-20261118T035517Z.json`）：`events-4` 以成本赢
被接受（`3356 -> 2139` tokens/report，−36.3%），每个录音的召回不变（`0.491 -> 0.491` / `0.037 -> 0.037`，
复制性通过），而新记录里现在有 `stream_path = data\smd\stream-check.json` —— 下一轮起，拒绝自己带着
自己的位置出生。

**边界 101：一份拒绝必须记住它是在哪个文件上被测量的，而不只是哪个数据集。** 只记名字时，复核要靠操作者
的记忆，而人会去测"最吵的那条流"，不会去测"最老的那条异议"。

**边界 102：缺输入时要拒绝，不要从名字推断。** 当一个数据集可能对应两个采样时，推断出的路径会静默地
换掉采样，然后把差别当成结论 —— 而它看起来像一次正常的复核。

### 3.54 承诺也要记住自己的流：一个数据集不是一个文件

第 46 轮让**拒绝**记住自己被测量在哪个文件上；**版本**仍然只有一份数据集摘要。于是 `--verify-version 20`
能不能立在承诺自己的录音上，取决于操作者还记不记得那是哪个文件 —— 而这个仓库里 `SMD` 是两个文件、采样不同
（`data/smd/stream.json` 948 窗口 / 828 打分 / 3000 秒，`data/smd/stream-check.json` 1355 / 1235 / 2100 秒）。
第 26 轮已经为此把 `v020` 判成 `unmeasurable` 一次：那次判定的主语其实不是承诺，是操作者选了哪个文件。

**修法**：版本文件记 `stream_path`（`RoundResult.stream_path` 由 CLI 在 `run_round` 之后写入，因为轮次读不到
CLI 参数），`Promotion.stream_path` 读回来；`--verify` 在**没有** `--stream-file` 时不再退回演示流，而是从记录
里解析：所有待查版本同意同一个文件就用它，有人没记文件、或者记录们互相矛盾，就列出来并拒绝。

```
$ brian evolve --verify --verify-version 19
1 version(s) do not say which stream file they were measured on: v019
pass --stream-file; a dataset name does not name a file, and guessing one would grade the promise on
whatever sampling happens to be at that path.                                     → 退出 2

$ brian evolve --verify --verify-version 20          （没有任何流参数）
stream file    data/smd/stream.json  (named by v020)
verifying 1 promotion(s) from smd on SMD
  v020  digest.scoring = 'auto', …, gate.rank_probability = 0.85  holds
        recall 0.507 at the 10% budget against a promised 0.507, 3358 tokens per report against 3358
```

第二条命令是这一轮的目的：同一条承诺，第 26 轮要靠操作者写 `--stream-file data\smd\stream.json` 才能复现，
现在它自己站对了地方，并且复现得分毫不差（0.507 / 3358 对 0.507 / 3358）。

**旧记录**：`v020` 的摘要写着 948 窗口 / 828 打分，磁盘上恰好一个流文件是这个几何 —— 补字段的脚本按**几何**
匹配、零个或多个匹配就什么都不写（`var/backfill_version_path.py`）。同一条路用在 `v019` 上却失败了：

```
$ python var/backfill_version_path.py var/evolve/versions/v019.json
recorded       source='RCAEval RE1-OB' windows=543 scored=456
  data/rcaeval/stream-holdout.json   windows=543  scored=456  cadence=60.0   match
  data/rcaeval/stream.json           windows=543  scored=456  cadence=60.0   match
no match: 2 file(s) have that geometry, so nothing is written
```

RCAEval 的原始流与留出流是同一批案例、同样的采样密度，几何完全一样 —— **几何也无法恢复文件**。这就是为什么
字段必须在写记录的那一刻记下来，而不是事后补：能补的是运气，补不了的是常态。

三条读法：

1. **一个数据集不是一个文件。** 摘要里写 `SMD` 只说明承诺关于哪批机器，不说明它关于哪个采样；改采样就是改问题，
   而 `unmeasurable` 会把它记成"承诺没通过"。
2. **回退到演示流是最坏的一种"有答案"。** 没有 `--stream-file` 时旧实现跑合成演示流，于是每个版本都被拿去和
   一段它从没见过的录音比 —— 报告里有数字、有判词，只是那句话不是关于这条承诺的。
3. **补字段要按事实补，不能按名字猜。** 按几何匹配、零个或多个匹配不写，是这一条的可执行形式；猜一个路径的
   脚本会把"我找不到"变成"就是它"，然后让一次复核看起来正常。

**边界 103：一条承诺必须记住它立在哪个流文件上。** 只记数据集时，复核只能靠记忆，而 `unmeasurable` 记录的
是操作者选了哪个文件，不是承诺本身；一个判词的主语错了，它就不该进账本。

**边界 104：记录们对"该用哪个流"有分歧时，列出来并拒绝。** 挑一个不是效率，是偷偷决定哪条承诺被评判 —— 而
读者会以为被评的是全部。拒绝要说出分歧本身：哪个文件、哪些版本。


### 3.55 按数据集分组不是按文件分组：一张表加总两次采样

`--campaign` 的行是按**数据集**分的，这是对的：部署状态就是按数据集存的（第 22 轮）。但数据集不是文件 ——
`SMD` 在这里是两个采样（`data/smd/stream.json` 948 窗口 / 828 有标签 / 3000 秒一个窗口；`stream-check.json`
1355 / 1235 / 2100 秒）。一行把两次采样的轮次加在一起，得出的数字回答了两个问题，而页面从来不说。

第 47 轮给承诺记了文件，第 46 轮给拒绝记了文件 —— 记录有了，报表没读。修法是让页面自己说出它的文件：

```
SMD                    36    177     80 20        recall 0.507 at the 10% budget, 3358 tokens/report
  measured on       data\smd\stream-check.json (1 round(s)), 35 round(s) without a recorded file
```

四件事一起做：

1. **每行记文件**（`CampaignRow.files`，按文件数轮次）。没有任何轮次记过文件的行写
   `measured on   %d round(s), none of which say which file they measured` —— "不知道"是一种答案，空着不是。
2. **`(not recorded)` 单独计数**，不并进任何一个文件：把没有记录的轮次算到某个文件头上，等于替它猜了一次采样。
3. **一个域用到多于一个文件时报警**：行下加 `! %d files here were sampled differently: this row's totals add up
   more than one question`，并在表头加一行 `stream files   %d domain(s) add up rounds measured on more than one file`。
   今天的账本还没有一行会触发它（历史记录早于这个字段），所以用一个合成的两文件账本 `var/demo_files48.py` 证明它
   真的会响 —— 第 79 条边界：一个在今天的数据上从不触发的保护，仍然要在某处证明它工作。
4. **`--sensitivity` 读更具体的记录**：拒绝的数字来自轮次，但文件的权威来源是第 46 轮写进 `w###` 记录的那个字段。
   只读轮次时，同一份拒绝在 `--withheld` 里写着 `data\smd\stream-check.json`，在敏感度表里写着"没有记录" ——
   两个页面就同一件事互相矛盾，比两个都不写更糟。现在先读轮次、再退回记录，并在行里写明用的是哪个文件：

```
domain                  refused  with nums    0.050    0.100    0.200
---------------------------------------------------------------------
SMD                           7          6        0        0        7
  measured on       data\smd\stream-check.json (7)
```

这张表读一遍：**行按数据集分是因为部署状态按数据集存，不是因为采样相同**；`(not recorded)` 必须自己一栏，
因为它比任何具体文件都更常见（今天 54 轮里 53 轮没有记录）；而两个页面必须读同一条更具体的记录，否则读者会
以为它们中的一个在说谎。

**边界 105：一张把多个流文件加在一起的表必须说出来。** 加总两次采样得到的数字同时回答两个问题（"在 3000 秒窗口
的采样上变好了多少"和"在 2100 秒窗口的采样上变好了多少"），而页面正是读者据以决策的地方。

**边界 106：同一个事实有两个记录时，取更具体的那个，并说明取的是哪个。** `w###` 记录比产出它的轮次更具体
（它就是为了回答"这份拒绝是在哪个文件上成立的"而写的）；不这样读，两个页面会就同一份拒绝给出不同的文件。


### 3.56 比例要说在读者看得见的地方：98% 的数字落不到某个采样上

第 48 轮让每一行说出自己的文件。行里有了，表头没有：

```
rounds            54  (228 candidate measurements, 101 accepted, 127 rejected)
...
file unknown      53 of 54 round(s) do not say which file they measured, so 98% of the totals above cannot be
                  placed on a sampling
```

这不是脚注。`SMD` 的两份记录采样不同，而上面 54 轮里 53 轮写在第 47 轮给轮次加 `stream_path` 字段之前 ——
所以"54 轮"这个数字里，几乎每一轮都可能是两份采样的混合。第 48 轮把这个事实写进了行尾的
`35 round(s) without a recorded file`，但表头仍然把它读成一份完整的账。

同一轮里补的另一半：`--history` 的行尾现在写明文件。

```
56  2026-11-18T03:55:22+00:00  accepted=events-4  recall@b 0.297 -> 0.297  tokens/report  3356 -> 2139
    [recorded, not applied]  [data\smd\stream-check.json]
45  2026-11-18T02:12:42+00:00  accepted=events-16,events-9,…  tokens/report  3356 -> 1987
    [recorded, not applied]  [no file recorded]
```

两件事合起来读：

1. **只在行里说的比例，等于没说。** 读者先看表头；表头写"54 轮"、行尾写"35 轮没有文件"，两者的差
   不会自己浮出来。比例必须和它修饰的那个数字在一起。
2. **写出来的债只能靠重跑下降。** 这些轮次确实测过，只是没记文件；补记等于凭空发明一次测量（第 46/47 轮
   两次拒绝过这种事）。所以 `98%` 会一直在页面上，直到新轮次在各自的文件上重跑，把比例换下去 ——
   页面必须扛住这个数字，而不是把它藏进行尾。
3. **两个页面必须用同一种方式回答同一个问题。** `--history` 不问文件，读者就得回 `--campaign` 逐行读；
   而更常见的结局是读者拿数据集的名字当文件的名字 —— 正是第 46、47 轮拒绝的那种猜测。

**边界 107：一张加总轮次的页面必须说出其中有多大比例不可归属。** 一个总数，如果它的组成部分不知道自己
属于哪个采样，那它就不是任何东西的测量值 —— "98% 不可归属"不是一张坏报告，而是一张诚实的报告。

**边界 108：历史页和战役页必须说出同一批事实。** 读者为了知道一行加总了哪个文件而不得不打开另一个命令，
他不会打开；他会假定数据集的名字就是文件的名字。


### 3.57 数字要能进闸门，也要能自己变小：98% 的两种去向

第 49 轮把比例印在表头上。印完之后它还是什么都不做 —— 一个没有退出码读取的数字是装饰。这一轮给它两条去向：
**让它能被要求**（`--max-unrecorded`），和**让它能被减少**（`--unrecorded` 列出仍然影响决策的那几轮）。

```
$ brian evolve --audit --strict --max-unrecorded 0.25     # exit 1
98% of the recorded rounds cannot be placed on a sampling, above the 25% asked for:
re-run the rounds whose totals still decide something, on the file each one measured.
```

`--max-unrecorded` 是**可选**的，默认不设 —— 原因写在实现里，也是这一轮自己的一个失败测试逼出来的：

> 本构建写的、没有文件的轮次，不一定是缺陷。`brian evolve` 不带 `--stream-file` 时测的是**生成出来的**
> demo 流，它没有文件可写。把这种轮次算成债务，会让 demo 域永远红着 —— 正是第 33 轮修过的那个失败
> （一个总是红的闸门等于没有闸门）。

所以默认只警告，比例上限由部署方自己定。默认警告是这样的两句：

```
53 round(s) predate the stream-file field, so which sampling their totals belong to is not recorded.
Re-running a round writes a record that says; --unrecorded lists the ones that still decide something.
```

第二条去向是这一轮真正的工作量所在。把 53 轮全部重跑没有意义，所以 `--unrecorded` 只列出**仍然在决定什么**
的轮次 —— 写出过还在页面上的版本，或者它还压着一份仍然被持有的拒绝：

```
rounds            53 of 54 cannot be attributed; 17 of them still decide something

domain             round                              why it still matters
------------------------------------------------------------------------------------------------
SMD                round-20261117T203743Z.json        wrote 4 version(s) still on the page
SMD                round-20261118T000310Z.json        wrote 1 version(s) still on the page; 6 refusal(s) still carried
SMD                round-20261118T022144Z.json        1 refusal(s) still carried
```

三件事一起读：

1. **闸门只收费它能说清的那部分。** 历史欠账和本构建的欠账要分开（第 33 轮的做法，这里换了个字段），
   而"本构建写的没有文件"还要再分一层：生成流没有文件可写。一个分不清这两者的闸门，会先被人静音，再被人删掉。
2. **比例要能被要求，就不能只印。** `--max-unrecorded 0.25` 让"不可归属超过四分之一"成为一个失败的构建，
   这样它和"有判词会变"（第 15 轮）、"有拒绝没人复核"（第 45 轮）一样，是同一类东西：能被机器读的断言。
3. **债要能变小，而且只变小值得变小的那部分。** 53 轮里 17 轮仍然在决定什么；其余 36 轮是没人再问的问题。
   把重跑的成本压在 17 轮上，比例才会真的从 98% 往下走 —— 而这不是把数字藏起来，是把数字变成工作。

**边界 109：一个印出来的比例必须能被某条命令要求，也必须能被某条命令减少。** 不能要求的比例是装饰；不能减少的
比例是抱怨。两者都会让读者学会忽略它。

**边界 110：欠账要按"谁还能还"分类，而不是按"欠了多少"一行带过。** 历史欠账（记录早于字段）和本构建的欠账
（生成流没有文件）不能合并计数，否则闸门要么永远红，要么永远绿 —— 两种都等于没在检查。


### 3.58 缺的那条信息就在记录里：把"回想"变成"选择题"

第 50 轮把"98% 不可归属"变成一条可以被要求、也可以变小的债。变小的方法在文档里写得很轻：重跑那些仍
然在决定什么的轮次。做的时候才发现，那句话漏了一步 —— 重跑需要 `--stream-file`，而"它测的是哪个文件"
恰恰是记录丢掉的那条信息。于是命令把最难的一步留给读者，还叫做"可以变小"。

这一轮不要求任何人回想。每一轮的记录里已经写了它看到的几何：多少个窗口、多少个被评分、事件数、录音名
—— `summarise()` 从第 2 轮起就写这些。文件那边同样有一份摘要。两边对得上，文件就是候选。对不上，就
什么都不提。

```
$ brian evolve --unrecorded
rounds            53 of 54 cannot be attributed; 17 of them still decide something

domain           round                            likely file                        why it still matters
SMD              round-20261117T201046Z.json      data/smd/stream.json               wrote 1 version(s) still on the page
SMD              round-20261117T203743Z.json      data/smd/stream.json               wrote 4 version(s) still on the page
SMD              round-20261117T230958Z.json      data/smd/stream.json               wrote 4 version(s) still on the page; 1 refusal(s) still carried
RCAEval RE1-OB   round-20261117T232141Z.json      data/rcaeval/stream.json           wrote 3 version(s) still on the page
SMD              round-20261118T011544Z.json      data/smd/stream-check.json         6 refusal(s) still carried
SMD              round-20261118T022144Z.json      data/smd/stream-check.json         1 refusal(s) still carried
…（17 行，每行恰好一个候选文件）
```

**17 行里有 17 行能放到一个文件上。** 10 行落在 `data/smd/stream.json`（第 22 轮之前的采样），7 行落在
`data/smd/stream-check.json`（之后换的采样），2 行 RCAEval 落在 `data/rcaeval/stream.json`。也就是说，
98% 的不可归属**不是数据里缺的，是页面上缺的**：账本一直知道自己测的是哪一份流，只是从来没地方写下来。

三件事值得记下来。

**一、几何能匹配到窗口，名字才能匹配到案例。** 最初的版本只比窗口数和评分数，两个 RCAEval 文件因此永远
并列 —— 它们都是 543 个窗口、456 个被评分、60 秒一拍，区别只在于装的是哪几个案例（`adservice_cpu_1..3`
还是 `_4..5`）。第一版把这两行印成 `2 candidates: …`，看起来诚实，其实是把第 47 轮"几何认不出文件"的
结论又抄了一遍。加上录音名（记录里本来就有）之后，两行都唯一了。规则是：**两侧都记了才比**，没记的字段
跳过而不是判为不等 —— 否则第 27 轮之前的记录（没有 `cadence_s`）会被一个它们不可能有的字段淘汰。

**二、候选不是写入。** 匹配到的文件印在表里，不写进任何记录，也不自动重跑。这条界线沿用第 46/47 轮：
把猜测写成事实，比不猜更糟。重跑一次，新记录自己带上 `stream_path`，这一行就从列表里消失 —— 变小是
重新测量换来的，不是打印换来的。

**三、一个候选和零个候选都要说话。** 零个候选印 `unknown` 而不是留空：空白读起来像"没人看过"，而
`unknown` 说的是"这一轮测的东西，本检出里没有文件对得上"，那是关于这一轮的事实。这条对应第 50 轮
边界 109 的另一半：能要求、能变小，还要能读懂哪里仍然不知道。

**边界 111：记录里已有的信息，必须在页面上出现，否则"缺数据"只是缺一列。** 三轮（第 49、50、51）都在
同一件事上打转：不可归属的比例印出来了、能被要求了、能被减少了，而真正让它减下去的，是那三列一直都在
记录里、只是没被打印出来的字段。

**边界 112：匹配要按记录实际拥有的字段做，缺字段不等于不相等。** 拿一个新字段去筛旧记录，会把它们全部
判为"不匹配"，然后"没有候选"看起来像结论，其实是字段年龄。


### 3.59 候选文件要变成一次测量：而且必须测记录里的那个问题

第 51 轮把"这一轮测的是哪个文件"从回想变成了选择题，17 轮全部落到唯一文件上。但它仍然只打印。让比例变小
的那一步——重跑——还得人一条条敲。这一轮把它变成命令。

第一版做错的地方值得写下来。`--rerun-unrecorded` 最初的做法是：拿到候选文件，然后**像平常一样跑一轮**，
再把旧记录退役，指向新记录。跑完的输出是：

```
retired round-20261117T201046Z.json (re-measured as round-20261118T041856Z.json)
  none of its 1 trial(s) appear in the successor: this round is retired as a question, not answered
```

退役机制自己说了实话：新那轮测的是**今天的提案**，不是记录里的候选。于是"欠账变小"变成了"旧问题被划掉、
换成一个新问题"——这是第 34 轮退役语义存在的原因，也正是它当场抓住的东西。数字从表上消失，问题没人回答。

正确的做法在记录里：每一轮的 `trials` 都存了候选的完整定义（`name` / `hypothesis` / `changes` / `veto`），
`Candidate.from_dict()` 能把它读回来，`run_round(selected=…)` 接受显式候选表。所以重跑就是：**把记录里的
候选原样再测一遍，测在它自己记录的文件上**。

```
$ brian evolve --rerun-unrecorded --rerun-limit 1
candidates        17 of 17 round(s) have exactly one file; 0 cannot be placed
limit             1 of the 17 will be re-run here; the rest keep their place until the command runs again

---- re-running round-20261117T201046Z.json on data/smd/stream.json
candidate                 recall@b   fpr@b  tok/rep   tok/inc evidence verdict
baseline                     0.507   0.091     3358      7419     100% -
host-scoring-deviation       0.507   0.091     3359      6861     100% accept

recorded round var\evolve\round-20261118T042134Z.json
retired round-20261117T201046Z.json (re-measured as round-20261118T042134Z.json)
  1 of its 1 trial(s) appear in the successor: host-scoring-deviation
file unknown      52 of 55 round(s) still cannot be placed — 16 left by the limit above
```

这一次重叠是 **1 of 1**：同一份候选、同一个文件、同一套规则。旧记录的总额离开表格，新记录带着
`stream_path` 进场，读者能顺着它找到测量条件。欠账的下降来自重新测量，而不是来自删除。

三个设计选择：

**一、默认只重跑一轮。** 一轮是几分钟的测量（948 个窗口、8 个候选、加基线）。一次敲键就跑 17 轮不是"可变
小"，是把一整小时的账单藏进一个动词里。`--rerun-limit N` 由调用方决定花多少，命令每次都报还剩几轮。

**二、候选文件不唯一就跳过，并说出跳过了什么。** 两个文件几何相同的时候（本检出里的两个 RCAEval 文件就是），
挑一个等于替读者决定这道题该在哪次采样上回答（边界 104）。命令把两行都印出来，等 `--stream-file`。

**三、重跑不部署任何东西。** 沿用第 44 轮（边界 95）：验证可以清掉一条反对，但不能顺手把改动推上线。重跑只写
测量记录，`write_versions=False`，部署状态一个字节都不动。

**边界 113：重跑必须测记录里的那个问题，否则退役只是把旧问题划掉。** 第 34/35 轮的退役重叠检查在这里第二次
发挥作用：它当场读了 `none of its trial(s) appear` 这句话，而这句话正是缺陷的样子。

**边界 114：一个会花钱的命令默认只能花一次。** "可以变小"必须写成一个可以按次数调用、每次都报余额的操作，
而不是一个动词触发的整段预算。


### 3.60 一次一轮的欠账要能看到余额：队列、投影，以及"什么才算还清"

第 52 轮把"候选文件"变成了一次重跑，但每次只还一轮。一个一次一轮的命令如果不说还剩多少、也不说跑完会
是什么样，读者得到的印象就是"什么都没发生"——它花了四分钟测量，页面上那个 95% 只动了 2 个百分点。

这一轮给队列加了余额与投影：

```
$ brian evolve --unrecorded
rounds            52 of 55 cannot be attributed; 16 of them still decide something
queue             16 round(s) can be re-run here; 1 already re-measured; the share would fall 95% -> 65%
```

```
$ brian evolve --rerun-unrecorded --rerun-limit 1
candidates        16 of 16 round(s) have exactly one file; 0 cannot be placed
progress          1 round(s) already re-measured into a named successor; 16 still to do
projection        if the rest are re-run the unrecorded share falls 95% -> 65% of 55 round(s)
limit             1 of the 16 will be re-run here; the rest keep their place until the command runs again

---- re-running round-20261117T203743Z.json on data/smd/stream.json
… （表格与判决，然后）
recorded round var\evolve\round-20261118T043112Z.json
retired round-20261117T203743Z.json (re-measured as round-20261118T043112Z.json)
  9 of its 9 trial(s) appear in the successor: events-8, events-4, reuse-300, analyze-pages-only, …
  progress         2 of 16 re-measured; 14 left, share 93% -> 67% if the rest run
file unknown      51 of 55 round(s) still cannot be placed — 15 left by the limit above
```

这一轮的重叠是 **9 of 9**：九个候选全部被重测，问题原样被回答了一遍。旧记录离开表格，新记录带着
`stream_path` 进场，那个 95% 掉了 2 个百分点——但页面上现在写清了它为什么只掉 2 点，以及继续跑会掉到哪。

三个设计选择：

**一、进度从账本推导，不另存一个进度文件。** 一份记录"已还清"的定义是：**它被退役进了一个会说出文件的继任者**。
退役到一个仍然说不出文件的继任者不算（那正是第 52 轮第一版的缺陷），没写继任者也不算。第二个状态文件会长出
自己的不一致："账本说做完了、测量说没做"，而这份代码的价值恰恰是让两句话必须同时为真。

**二、投影只承诺队列能承诺的东西。** 每重跑一轮，退休一条无名记录、新增一条有名记录，总数不变、分子减一。
所以"全部跑完"的终点是可以用这两个数算出来的：`(无名 − 可重跑) / 总数`。它不是一个预测（剩下的轮次可能被
跳过，可能读不回来），只是"如果都跑完"的算术。

**三、投影必须与"还清"的定义共用一套判定。** 同一个函数同时喂 `--unrecorded` 的 `queue` 行和
`--rerun-unrecorded` 的 `progress`/`projection` 行；两处若各写一份，读者会在两页上看到两个余额。

**边界 115：一个一次一轮的还款命令必须报余额与终点。** 只说"重跑了一轮"等于让读者自己算还剩多少——
而人不会算，人只会觉得没进展。

**边界 116：进度必须从账本推导，不能来自第二个记账文件。** 两处记账迟早会不一致，而"已还清"与"已回答"
一旦分开，报表就会说出比测量更多的话。


### 3.61 整队一次跑完：余额印在第一次测量之前，第一次失败就停

第 53 轮让队列报余额，但仍然一次一轮。欠账要真的降下来，操作员得把同一条命令敲十四次——而人会敲三次然后
去干别的。这一轮让 `--rerun-limit 0` 表示整队，并把"这是整队"这句话印在**第一次测量之前**。

队列的余额也从命令输出走上了战役报表——读者决定要不要继续花时间，读的往往是那张表：

```
$ brian evolve --campaign
rounds            55  (236 candidate measurements, 108 accepted, 128 rejected)
…
file unknown      51 of 55 round(s) do not say which file they measured, so 93% of the totals above cannot
                  be placed on a sampling
queue             14 round(s) can be re-run from the record; 2 already re-measured; all of them would take
                  the share 93% -> 67%
```

两者用的是同一个函数：报表说"全部跑完会到 67%"，命令说"还剩 14 轮"，说不一致的时候，读者只会相信更
乐观的那个。

整队的开头长这样：

```
$ brian evolve --rerun-unrecorded --rerun-limit 0
candidates        14 of 14 round(s) have exactly one file; 0 cannot be placed
progress          2 round(s) already re-measured into a named successor; 14 still to do
projection        if the rest are re-run the unrecorded share falls 93% -> 67% of 55 round(s)
limit             draining all 14 round(s); the queue stops at the first round that cannot be re-run
```

三个设计选择：

**一、整队不是默认。** 第 52 轮把默认定成一轮，理由是"一轮是几分钟测量"（边界 114）。整队是**显式**指令：
调用方说了要花这笔钱。默认与显式的区别就是"意外花掉"与"请求花掉"的区别。

**二、第一次失败就停。** 一轮重跑失败意味着：记录读不回来、测量没能写出、或退役没写进账本。此后继续跑，
花掉的是同样的钱去证明同一件事——一个坏掉的步骤会连续失败十四次。停下来并保留其余轮次的位置，命令可以
再跑一次。

**三、余额必须在第一次测量之前印出来。** 一个人敲下"整队"时应该先看到"14 轮"和"跑完会到 67%"，而不是在
输出末尾发现。这条规则的理由是：命令的输出是**读者的预算表**，不是它的执行日志。

整队跑完的样子（摘要）：

```
$ brian evolve --rerun-unrecorded --rerun-limit 0
…
  progress         16 of 16 re-measured; 0 left, share 67% -> 67% if the rest run

$ brian evolve --campaign
retired           18 round(s) were re-measured; their records stay, the checks skip them
file unknown      37 of 55 round(s) do not say which file they measured, so 67% of the totals above
                  cannot be placed on a sampling
queue             0 round(s) can be re-run from the record; 16 already re-measured; all of them would
                  take the share 67% -> 67%
```

`93% -> 67%` 是估计，`67%` 是结果——估计与结果相同不是因为预言准，而是因为两者用的是同一个函数：投影
算的是"能跑的都跑完会到哪"，跑完之后剩下的就正好是那个数。

整队还顺手暴露了一个缺陷：报表说 SMD 行"这里有 3 个文件被不同采样"，而实际上只有两个——因为一条记录写的是
`data\smd\stream-check.json`（Windows 反斜杠），另一条写的是 `data/smd/stream-check.json`（命令里粘贴的
正斜杠）。同一个文件被当成两个文件，于是"这一行的总数在加总两次采样"这句话本身也变成了错的。修法是在数文件
之前把反斜杠归一成正斜杠，报表与敏感度表用同一个归一化（第 48 轮的两页一致性规则）。

**边界 119：一个文件只有一个名字。** 同一个路径的两种拼写会在报表里变成两次采样，而"几个文件"正是读者用来
判断这一行能否相减的依据。

**边界 117：批量在第一次失败处停下，并说出来。** 一个坏步骤之后的批量不是批量，是用剩下的预算反复证明
同一个失败。

**边界 118：显式的"全部"仍然是一条指令，它的代价要在第一次测量前亮出来。** 默认一轮、显式整队，这个区别
就是"意外花掉"和"请求花掉"。


### 3.62 欠账在下降，还是在被新记录冲淡

第 54 轮把余额印上了战役报表，也把队列印上了："14 轮可以重跑，跑完从 93% 到 67%"。但它回答不了另一个
问题：这个比例**动过没有**。一个读者决定要不要再花一小时测量，需要的正是这个；一个数字说"还剩 67%"，不
说它是从 95% 降下来的，还是从 67% 涨上来的。

记录里其实两件事都写了：一轮在 `started_at` 到达，在 `retired.at` 离开。于是任意过去时刻的比例都可重算：

```
$ brian evolve --campaign
file unknown      37 of 55 round(s) do not say which file they measured, so 67% of the totals above
                  cannot be placed on a sampling
queue             0 round(s) can be re-run from the record; 16 already re-measured; all of them would
                  take the share 67% -> 67%
trend             the unrecorded share fell 33 point(s) since the first recorded round 10.8 hour(s) ago
                  (100% -> 67%): 18 round(s) were retired after a named re-measurement and 73 record(s)
                  arrived in that window; the history is shorter than a day, so no rate is claimed
```

`debt_history()` 从账本重建这条曲线，不写第二个进度文件（边界 116）：一个单独的进度文件能说出与账本不同
的故事，而读者无从判断哪个是真的。它只在内存里按事件顺序重放，所以"多少轮被付清"永远是账本自己的答案。

**建的时候撞到的缺陷：窗口比账本还长。** 第一版请求"最近 7 天"，而这本账只有 0.45 天，于是"7 天前"是一个
空账本，比较对象是"什么都没有"，输出变成"比例没有变化"——一笔明明刚被付掉的债，在报表上显示为没动。修法
是把基准换成**记录能命名的最早状态**（第一条记录之后），并且把 `truncated` 带出来，让句子说"自第一条记录
以来 10.8 小时"，而不是"最近 7 天"。一句话说得比证据长，就是这句报表的第一次撒谎。

**整队的收尾。** 一条命令付掉几轮欠账，应该结束时说自己付了什么：

```
drained           1 round(s) retired in this invocation; 0 still in the queue; the unrecorded share
                  68% -> 66%
```

这行也是**推导**出来的：每次退役自带时刻，所以它就是账本事件的另一个视角，不是第二本账。

**边界 120：不到一天的记录不报速率。** 一次在几分钟内跑完的整队能说"动了 33 个百分点"，不能说"每天 33 个
点"——把一下午写成速率，就把它变成了一句承诺（第 40 轮"容忍度是个假设"的时间版）。

**边界 121：比例下降可能来自分子，也可能来自分母。** 一条只说"下降了"的曲线，在记录变多时同样会下降。所以
那行必须同时印出它用到的两个计数：付清了几轮、新到了几条记录。


### 3.63 趋势也要能进闸门：欠账变大是缺陷，但不能把演示流判红

第 55 轮让战役页说出"无法归属的份额"是升是降：从账本的到达事件和退役事件重算，不另开一个进度文件。第 56 轮的问题是**一个数字印在页面上、却没有任何退出码读它，就是装饰**（边界 98），而"欠账在变多"恰恰是整条补跑队列存在的理由——份额上升意味着新记录在增加、旧的却没人补。于是趋势进了闸门。

```
$ brian evolve --campaign --trend-days 0.5
file unknown      2 of 3 round(s) do not say which file they measured, so 67% of the totals above
                  cannot be placed on a sampling, 2 of them written by this build
trend             the unrecorded share rose 67 point(s) in the last 12.0 hour(s) (0% -> 67%):
                  0 round(s) were retired after a named re-measurement and 2 record(s) arrived in that window

$ brian evolve --audit --strict --trend-days 0.5 --max-share-rise 0
every recorded verdict survives today's rules.
66.7 point(s) of the unrecorded share came back in the last 12.0 hour(s): 0 round(s) were re-measured
and 2 record(s) arrived.
the share rose by more than the 0.0 point(s) asked for, so --strict exits 1.
re-run the queue, or write the rounds with --stream-file so they name a sampling.
[exit 1]
```

（这两条命令跑在一个三轮的合成账本上：一轮有名字、在 60 天前，两轮没名字、在刚过去的 20 分钟里。真实账本的份额正在下降，所以"上升"必须在一个能被观察的地方演示一次。）

**默认只警告，按需才失败。** 这个构建在测量生成的演示流时**故意**写没有文件名的轮次（第 50 轮）。如果"上升一律失败"，演示域就会永远红——一个永远清不掉的退出码，是没人会跑的检查（第 33 轮）。所以警告默认只说"这是一笔没人在还的账，想要它失败就传 `--max-share-rise`"，传了才收。

三个只有跑起来才会发现的缺陷：

1. **窗口印成 0.0 小时。** `--trend-days 0.0005` 时页面写 `in the last 0.0 hour(s)`——一个渲染成零的跨度读起来就是"没有时间"，而关于"没有时间"的收费，读者无从反驳。改成按长度选单位（天 / 小时 / 分钟 / 秒），`span_text()` 同时供页面和闸门使用，避免两处各写一份。
2. **设了上限、却没超，也印"超过上限"。** 第一版只看 `max_rise is not None` 就印"rose by more than … so --strict exits 1"，而退出码是 0：一句话和自己下面三行的退出码互相矛盾。现在只有真的超了才印那句，没超时印"rose by less than the 0.0 point(s) asked for, so --strict does not charge it"。
3. **低于容差也能收费。** 上升 0.3 个点（页面按整点打印，看不出变化）配 `--max-share-rise 0` 会让 `--strict` 退出 1，而关于它的段落被容差挡掉了——**为一件没说出口的事失败**，正好是边界 98 的反面。现在收费条件与打印条件绑在一起：`trend_rise > trend_tolerance and trend_rise > max_rise`。

三条读法：

- 页面按整点打印（67），闸门按一位小数（66.7）：前者给读者"份额"，后者要能收 0.5 这样的上限。同一条事实，两种精度，都写明了来源。
- 趋势是**算出来的**，不是记下来的：`debt_history()` 从账本的 `started_at` 与 `retired.at` 重建每一刻的份额，因此它不可能和账本不一致（第 53 轮的规则延伸到时间轴）。
- "上升"这件事在真实账本上从未发生过：一个从未在真实数据上出现过的守卫，仍然必须在能被观察的地方演示一次，否则它只是代码里的一段分支（边界 79）。

**边界 122：欠账变大是缺陷，不是页面上的一句描述。** 无人在还的债在增长，是闸门必须**能够**收费的失败，不能只由页面打印；一个只能被观察、不能被要求的数字，读久了就会被忽略（边界 109）。

**边界 123：只有当账本能归属这次上升时，它才可收费。** 这个构建在生成的演示流上按设计写没有文件名的轮次（第 50 轮），所以默认只警告，`--max-share-rise` 才是收费开关；收费的段落必须同时说出窗口里**还了多少**（退役后重测）和**来了多少**（新记录），否则"上升"无法区分"债务在增加"与"工程在推进"。


### 3.64 欠账在哪一流，这一轮该测哪里：把趋势变成输入

第 56 轮让"未归属比例在涨"能失败，但那一整页说的都是整本账的份额。`--campaign` 的 `file unknown 37 of
56 round(s) … 66%` 说明了有多少无法定位，却没说**欠在哪个流**；而 `--propose` 可以在完全不管欠账的情况下
跑一轮 `demo`——那一轮的记录本身也会不可归属，于是欠账又加一条。这不是打印不足，是决策没有输入。

修法分两处，都从账本算，不引入第二个记账文件：

- `_debt_by_domain(root)` 在每个流上数三件事：轮数、没有文件的轮数、其中**可重跑**的轮数（几何匹配唯一时
  才算）。"可重跑"必须单独数：一条几何匹配不上的欠账，重跑这条命令永远付不掉，把它算进可付总额只是把一个
  无法清偿的数字放在分子里。
- `_largest_debt(root)` 只在可付欠账里挑；`_print_debt` 在**提议之前**打印它，并在本轮没有
  `--stream-file`（即要测生成的 demo 流）时按边界 124 声明：这一轮会多一条不可归属记录，并且不付任何欠账。
- `_order_by_debt(root, ready)`：重跑队列按欠账排序，而不是按账本先后。

真实账本上，可付欠账已经是 0——第 54 轮那一整队把它跑完了，所以本轮只在 dry run 里读到那句更短的诚实答案：

```
$ python -m brian.cli evolve --propose
!                 no payable debt remains, and this round measures the generated stream, which has no file to
                  name: its record will be unattributable too (demo already has 1 of 1 unrecorded)
```

要让"最大可付欠账"这句话出现，需要一个还有欠账的账本，于是用 `var/debt57_demo.py` 造了一个两轮的小根
（一轮 SMD 写了版本、没记文件、几何唯一可匹配；一轮 demo 无文件），同一根上两条输出：

```
$ python -m brian.cli evolve --propose             # 在 var/debt57 下
debt              the largest payable debt is on SMD: 1 of 1 round(s) cannot be placed on a sampling
                  (100%), 1 of them can be re-run from the record
!                 this round measures the generated stream, which has no file to name: it adds one
                  unrecorded round, demo already has 1 of 1 unrecorded, and pays none of SMD's debt

$ python -m brian.cli evolve --rerun-unrecorded --rerun-limit 0
candidates        1 of 1 round(s) have exactly one file; 0 cannot be placed
debt              SMD              1 of 1 round(s) unrecorded (100%), 1 re-runnable
projection        if the rest are re-run the unrecorded share falls 100% -> 50% of 2 round(s)
```

三个读数：

1. **"最大"只在可付的集合里有意义。** 一个域可以 100% 不可归属却没有一条能重跑的轮次；把它叫"最大欠账"
   会让操作员去付一笔这条代码根本付不掉的账。
2. **没有可付欠账时仍要说话。** 真实账本上可付欠账是 0，但这一轮的记录**仍会**不可归属；沉默会让"没有欠账"
   与"这一轮不让它变大"看起来是同一件事，而它们不是。
3. **排序也是策略。** 按账本先后付，付的是"先被测的那个流"；按欠账付，付的才是"欠得最多的那个流"。第 54 轮
   的队列从头跑到尾看不出差别，一旦队列中途停下（边界 117），差别就是付了 8 轮还是付了 2 轮。

**边界 124：测一个说不出文件名的流，就是把不可归属的那一列加一。** 跑这一轮的命令必须在花掉这几分钟之前
说明这一点，并同时说出可付欠账在哪个域；否则循环会先制造它之后必须偿还的欠账，而且没有人被告知。

**边界 125：队列按什么排序，就是在按什么付账。** 按欠账排的队列付最大的那一笔；按账本先后排的队列付最早
测的那个流。中途会停的队列（边界 117）里，这两种排序付出的是不同的结果，所以排序必须写下来，而不是留给
`sort()` 的稳定默认。


### 3.65 按域的比例也要能进闸门：但只收能付的账

3.64 让页面说出"哪一流欠得最多"，这个数字仍然没有进闸门。欠账 100% 的一行，只要那几轮已经测出的
判决今天还站得住，`--strict` 就照样绿：**页面上印着一个读者必须自己判断的数字，而机器不判断它。**

修法看起来只是把 `--max-debt-share` 传给 `_strict_exit`，但真做起来要回答一个区别：

- **哪一行最难看**：`_debt_by_domain` 按 share 排序，第一行就是读者要面对的那一行；
- **哪一行的账有人能付**：`_largest_debt` 只返回 `ready > 0` 的域——`ready` 的意思是"这一轮的文件能由几何定出来，
  而且它今天仍然决定点什么"。

于是规则是：**印所有域的份额，只为能付的域扣分。**

```
$ brian evolve --audit --strict --max-debt-share 0.5          (真实账本)
6 verdict(s) would be decided differently today; 13 more kept their verdict but changed rule.
(unattributed) has 100% of its rounds unrecorded (1 of 1), above the 50% asked for:
no recorded round on it can be re-run (its candidates or its file are not in the record),
so --strict does not charge it: a gate that can never be cleared is a gate nobody runs.
exit=1        # 来自那 6 条翻案的判决，不来自欠账

$ brian evolve --audit --strict --max-debt-share 0.5          (var/debt57，能付的那种)
SMD has 100% of its rounds unrecorded (1 of 1), above the 50% asked for:
a row whose rounds are mostly unattributable cannot be read as a measurement, so --strict exits 1.
1 of those round(s) can be paid by re-running them on the file they measured.
exit=1
```

第一条是这一轮最重要的输出：**闸门诚实地报告了自己没有扣分，以及为什么。** 如果换成"SMD 55% 而且还能付"，
第一条就不会是 warning；真实账本上 SMD 的 55% 在 3.54 的排空之后已经付清，所以可付集合是空的，
最难看的那一行变成了 `(unattributed)`——它连候选定义都没留下，任何命令都付不了。

**第二半：队把自己付了什么写进记录。**

3.58 的排空只在终端里说"这次退休了 N 轮、份额从 X 降到 Y"。读者第二天打开账本，看见一堆
`round-2026…json`，无法分辨"瞄准了欠账的那一轮"和"顺手测的一轮"。于是 `RoundResult.queue` 把
这次付款写进它付掉的那一轮记录：

```json
"queue": {
  "paid": "SMD",
  "paid_round": "round-smd-unrecorded.json",
  "largest_at_start": {"domain": "SMD", "round": 1, "without": 1, "share": 1.0, "ready": 1},
  "share_after": 0.0
}
```

`largest_at_start` 是关键字段：它让"当时该瞄准哪一流"成为记录里的一个事实，而不是读者对时间线的推断。
现场（`var/queue58/`，一轮未记录、候选是 `events-4`、文件由几何定出）：

```
debt              SMD              1 of 1 round(s) unrecorded (100%), 1 re-runnable
limit             draining all 1 round(s); the queue stops at the first round that cannot be re-run
---- re-running round-smd-unrecorded.json on data/smd/stream-one.json
  1 of its 1 trial(s) appear in the successor: events-4
drained           1 round(s) retired in this invocation; 0 still in the queue; the unrecorded share 100% -> 0%
paid              SMD x1
balance           no payable debt remains: every remaining unrecorded round keeps no candidate definitions
                  or no unique file
```

对应的记录里 `"stream_path": "data/smd/stream-one.json"`，`"queue": {…}` 如上。**"paid SMD x1" 与记录里的
`queue.paid` 是同一个事实的两个出口**，一个给盯着终端的人，一个给明天打开账本的人。

三处只有跑起来才会暴露的细节：

1. **`to_dict()["queue"] = … or None`**：一条手跑出来的轮次不属于任何队列，写 `{}` 会被读成"这是一次
   付了零的队列条目"。空就是 `null`。
2. **`largest_at_start` 必须在循环之外取**：它在第一次测量之前算一次。若在每一轮之后重算，付掉第一域之后
   它就变成"当前最大"，记录里写的就不再是"这次命令瞄准谁"，而是"它自己刚刚造成的状态"。
3. **没有可付欠账时也要说话**：`balance no payable debt remains…` 明确把"付完了"和"付不了"分开——
   这两件事在数字上都是 0 轮可跑。

**边界 126：按域的比例是读者面对的那一行，所以它必须能被要求——但为一条没有任何命令能降下去的份额扣分，
就是那道永远红的闸门；印它，不扣它。**

**边界 127：会动手的队列必须把它付了什么、还剩什么写进它改过的那条记录，而不是只写进跑了它的那个终端。**

### 3.66 说出欠账在哪，还要说出付它的那一步

3.64/3.65 让页面点名"欠得最多的可付域"，并让 `--strict` 能按域扣分。但页面停在了**位置**上：
读者看到 `debt … SMD 14 of 27 …`，知道该测哪里，却不知道**下一条命令是什么、跑完会怎样**。
于是操作者按 `--rerun-limit 1` 跑一次、读 `balance`、再回到表格里用眼睛重排一次——
**两步的动作被拆成了三次阅读**，而每一次阅读都是一次可以放弃的机会。

修法：把队列自己的顺序印在债务旁边。

```
$ brian evolve --campaign                         # 两个可付欠账（var/steps59）
file unknown      2 of 2 round(s) do not say which file they measured, so 100% of the totals above …
queue             2 round(s) can be re-run from the record; 0 already re-measured; all of them would take the share 100% -> 0%
debt              the largest payable debt is on RCAEval RE1-OB: 1 of 1 round(s) cannot be placed on a sampling (100%), 1 of them can be re-run from the record
measure next      brian evolve --rerun-unrecorded --rerun-limit 1 pays RCAEval RE1-OB by one round; the command prints the balance at the end
then              SMD: 1 of 1 round(s) unrecorded (100%), 1 re-runnable
```

跑一次之后，同一页自己改口：

```
$ brian evolve --rerun-unrecorded --rerun-limit 1
debt              RCAEval RE1-OB   1 of 1 round(s) unrecorded (100%), 1 re-runnable
debt              SMD              1 of 1 round(s) unrecorded (100%), 1 re-runnable
---- re-running round-rcaeval-unrecorded.json on data/rcaeval/stream-one.json
  progress         1 of 2 re-measured; 1 left, share 50% -> 0% if the rest run
drained           1 round(s) retired in this invocation; 1 still in the queue; the unrecorded share 100% -> 50%
paid              RCAEval RE1-OB x1
balance           the largest payable debt is now SMD: 1 of 1 round(s) unrecorded (100%), 1 re-runnable

$ brian evolve --campaign
debt              the largest payable debt is on SMD: 1 of 1 round(s) cannot be placed on a sampling (100%), 1 of them can be re-run from the record
measure next      brian evolve --rerun-unrecorded --rerun-limit 1 pays SMD by one round; the command prints the balance at the end
```

注意第二次**没有 `then` 行**：只剩一个可付欠账时，页面不该暗示还有第二步。`then` 的存在与否本身是信息。

三条设计取舍：

1. **plan 用的是队列的顺序，不是按份额重排。** `_debt_plan` 直接取 `_debt_by_domain` 里 `ready` 的行，
   而 `_order_by_debt` 用的是同一个函数。页面说"下一次付 A"，命令就真的先付 A——**两套顺序会让那句话变成假话**
   （边界 129）。测试里专门有一条：即便 plan[1] 的份额更大，`measure next` 也必须点名 plan[0]。
2. **没有可付欠账时也要说话。** `measure next      nothing is payable by re-running: …`——否则读者看到
   没有 `debt` 行，会以为页面没算出来，而不是"没有可付的账"（边界 100 在另一个方向上的应用）。
3. **命令字符串来自同一处。** `measure next` 里的 `--rerun-limit 1` 与 `_rerun_limit` 的默认值是一个事实的
   两个出口；改默认值时两处必须一起改，否则页面会教用户一条不再按预期起作用的命令。

**边界 128：页面点名了一条债务，就必须点名付它的那一步——只写"欠在哪"的页面把"下一条命令是什么"
留给读者重推，而重推的每一步都是可以放弃的地方。**

**边界 129：页面里的"下一步"必须由命令真正使用的那个顺序生成；页面顺序与命令顺序不一致时，
"下一步"这句话就是假的，而且它比没有这句话更坏，因为它看起来经过了计算。**

### 3.67 把三句话合成一句预算：这一条命令跑几轮、份额落到多少

3.66 让页面点名"下一条命令"和"之后的那一步"，但读者仍然要自己把三句话拼起来：队列有多大（`queue`）、
下一步付谁（`measure next`）、全跑完落到多少（`queue` 里的 `share_after`）。**"我跑一次，会到哪？"
这个问题，页面从来没有一句话回答过。**

修法：`_queue_budget(root, limit)`——用**命令自己的两个函数**（`_rerun_limit` 与 `_rerun_progress`）
把这一次调用的入口和出口算出来，印成一行：

```
$ brian evolve --campaign                     # var/steps59，两个可付欠账
queue             2 round(s) can be re-run from the record; 0 already re-measured; all of them would take the share 100% -> 0%
debt              the largest payable debt is on RCAEval RE1-OB: 1 of 1 round(s) cannot be placed on a sampling (100%), 1 of them can be re-run from the record
measure next      brian evolve --rerun-unrecorded --rerun-limit 1 pays RCAEval RE1-OB by one round; the command prints the balance at the end
then              SMD: 1 of 1 round(s) unrecorded (100%), 1 re-runnable
budget            brian evolve --rerun-unrecorded --rerun-limit 1 pays 1 round(s) (RCAEval RE1-OB); the queue falls 100% -> 50% of 2 round(s)

$ brian evolve --campaign --rerun-limit 2
budget            brian evolve --rerun-unrecorded --rerun-limit 2 pays 2 round(s) (RCAEval RE1-OB, SMD); the queue falls 100% -> 0% of 2 round(s)

$ brian evolve --campaign                     # 真实账本：没有可付欠账
measure next      nothing is payable by re-running: every unrecorded round keeps no candidate definitions or no unique file
budget            nothing to spend: the queue has no round it can re-run, so the share stays 66%
```

`measure next` 与 `budget` 不是重复：前者是**一步**（大债务在哪里、先付谁），后者是**这一步的终点**
（跑几轮、付掉哪些域、份额落到多少）。一个读者想知道的"值不值得跑"只在后一句里。

**跑起来才发现的缺陷（第一版就是这么错的）**：`_queue_budget` 最初把 `limit=None` 解释成"能跑的都跑"，
于是两个欠账时页面印 `pays 2 round(s)`，而上一行 `measure next` 说 `--rerun-limit 1 pays ... by one round`。
**页面在跟自己争论。** 修法是把读者会打的那个数传进去：`budget_limit = _rerun_limit(args, ready)`，
并让 `command` 里写具体轮数而不是 `0`（`0` 是"全排空"的写法，页面上写成具体数字更好读）。

机器可读的那一半：`--campaign --json-out` 现在带 `debt_plan` 与 `budget` 两个派生字段。

```json
"budget": {"command": "brian evolve --rerun-unrecorded --rerun-limit 1", "domains": ["RCAEval RE1-OB"],
           "ready": 2, "rounds": 1, "rounds_total": 2, "share_after": 0.5, "share_now": 1.0}
```

三条设计取舍：

1. **预算与推荐的命令必须同源。** 都从 `_rerun_limit`/`_rerun_progress` 来，所以页面不可能承诺一个
   命令不会产生的移动（边界 130）。
2. **`--rerun-limit 0` 是"全排空"的一种写法，页面把它翻译成具体轮数。** 读者看到 `pays 2 round(s)`
   比看到 `--rerun-limit 0` 更容易判断成本；而 `command` 字段仍写具体数字，因为那才是这一刻的真实效果。
3. **派生数字必须同时出现在 JSON 里。** 否则脚本要么解析人类可读文本（改版即坏），要么自己重算
   （第二个真相）——两者都会让"页面说的"和"程序做的"慢慢分叉（边界 131）。

**边界 130：一条"下一步"命令必须附带它的结果预算——跑几轮、份额落到多少；否则读者要用三次阅读拼出
一次动作，而每一次阅读都是可以放弃的地方。**

**边界 131：页面上任何派生数字都必须同时出现在机器可读输出里；脚本只能解析人类可读文本时，
它的"真相"会随文案改版悄悄过期。**

### 3.68 一句预算要有时间，也要是同一句话

3.67 让页面回答了"跑一次会到哪"，但只说**轮数**：`pays 1 round(s) … the queue falls 67% -> 33%`。
"一轮"不是一个答案，除非读者已经知道一轮多久——而这个数正好在记录里，页面从来没印过。
同时，命令自己印的是另外两句（`projection` 与 `limit`），与页面推荐的句子措辞不同：
读者照着页面运行，看到的是另一句话，需要自己确认它们是同一件事。

修法两件：

1. **时间从中位数来。** `_round_minutes(root)` 取所有带 `started_at`/`finished_at` 的记录的
   墙钟时长中位数（不是均值：一次慢轮不该给整队定价），`_queue_budget` 再乘上这一轮要跑的轮数，
   得到 `estimated_minutes`。记录里没有时间戳的轮贡献 0 次，而不是"猜一个"。
2. **一句话只由一处产出。** 新增 `budget_text(budget)`，页面与命令**共用**它：命令在第一次测量之前
   印同一句 `budget`，而把原来"排空时会停在第一个无法复跑的轮"那句保留成单独的 `stops` 行
   （边界 118 仍然成立）。

```
$ brian evolve --campaign                      # var/budget61，两条可付欠账，中位数一轮 5 分钟
debt              the largest payable debt is on RCAEval RE1-OB: 1 of 1 round(s) cannot be placed on a sampling (100%), 1 of them can be re-run from the record
measure next      brian evolve --rerun-unrecorded --rerun-limit 1 pays RCAEval RE1-OB by one round; the command prints the balance at the end
then              SMD: 1 of 2 round(s) unrecorded (50%), 1 re-runnable
budget            brian evolve --rerun-unrecorded --rerun-limit 1 pays 1 round(s) (RCAEval RE1-OB); the queue falls 67% -> 33% of 3 round(s), about 5 minute(s)

$ brian evolve --campaign --rerun-limit 0
budget            brian evolve --rerun-unrecorded --rerun-limit 2 pays 2 round(s) (RCAEval RE1-OB, SMD); the queue falls 67% -> 0% of 3 round(s), about 10 minute(s)

$ brian evolve --rerun-unrecorded --rerun-limit 0     # 命令自己印的那一句，与上面逐字相同
candidates        2 of 2 round(s) have exactly one file; 0 cannot be placed
progress          0 round(s) already re-measured into a named successor; 2 still to do
budget            brian evolve --rerun-unrecorded --rerun-limit 2 pays 2 round(s) (RCAEval RE1-OB, SMD); the queue falls 67% -> 0% of 3 round(s), about 10 minute(s)
stops             at the first round that cannot be re-run; the rest keep their place
```

机器可读的那一半同样带两个新字段：

```json
"budget": {"command": "…--rerun-limit 1", "domains": ["RCAEval RE1-OB"], "estimated_minutes": 5.0,
           "minutes_each": 5.0, "ready": 2, "rounds": 1, "rounds_total": 3,
           "share_after": 0.3333, "share_now": 0.6667}
```

**造这个证据时踩到的两个坑，都是"演示根目录"的坑**（值得记下，因为它们会伪装成产品缺陷）：

1. 两个演示流文件的几何完全一样，于是**每一条记录都同时匹配两个文件**，`ready` 是 0、`blocked` 是 2，
   页面说"没有可付欠账"——这与 3.58 里两个 RCAEval 文件并列的情形是同一件事，只是这次是我自己造的。
2. 第一版演示记录写了 `"stream_path": null` 这个**键**、却没写版本文件，于是轮次既不算"有文件"
   也不满足"仍在决定什么"（写了仍在页面上的版本或被携带的拒绝），`_unrecorded_matters` 根本不列它。
   记录的形状要按 `persist` 真正写出来的样子来造，否则测的是夹具不是代码。

**边界 132：一句预算必须包含它要花的时间——"三轮"不是时间；估法必须来自记录（中位数），
否则它是猜测，而猜测会在第一次慢轮之后过期。**

**边界 133：页面推荐的句子与命令打印的句子必须由同一个函数产出；各自拼接时，改一处就会让读者
照着建议运行、却看到另一句话。**

### 3.69 同一队里两个域的轮不是一个价：把等待按域算，并让它能被拒绝

第 61 轮给整队加了一个时间预算，但那是一个**全域中位数**：一条同时付 SMD（两台主机、一段长录制）
和 RCAEval（十五个用例）的队，不是"每轮大约六分钟"。读者在决定要不要等，等的是**总和**；
总和只有在每个域按自己的轮定价时才成立。而"等"本身也应该能被部署拒绝——一个二十三分钟的
重跑队列不该只能靠读者自己心里说一句"太久了"。

三处改动：

| 位置 | 改了什么 |
| --- | --- |
| `_timed_lengths` / `_round_minutes_by_domain` | 一次解析，两个速率：全域中位数仍在，另按 `stream.source` 分组给每个域自己的中位数 |
| `_queue_budget` | 走队时同时累加每个域的轮数 → `minutes_by_domain`；代价是**和**而不是"单价 × 轮数"；某个域没有计时的轮就退回全域中位数，而不是编一个 |
| `budget_text` / `_strict_exit` | 句子里写总和（`%g` 分钟），并且**只有付两个以上域时**才在括号里给明细；`--max-minutes` 让它进闸门 |

页面（`var/budget61`，SMD 的轮 4 / 5 分钟、RCAEval 的轮 6 分钟）：

```
queue             2 round(s) can be re-run from the record; 0 already re-measured; all of them would take the share 67% -> 0%
measure next      brian evolve --rerun-unrecorded --rerun-limit 1 pays RCAEval RE1-OB by one round; the command prints the balance at the end
budget            brian evolve --rerun-unrecorded --rerun-limit 2 pays 2 round(s) (RCAEval RE1-OB, SMD); the queue falls 67% -> 0% of 3 round(s), about 10 minute(s) (RCAEval RE1-OB 6, SMD 4.5)
```

闸门（`--campaign --strict --max-minutes 5`）：

```
the next queue run would spend about 10.5 minute(s), above the 5.0 asked for:
lower the budget with --rerun-limit, or re-run the queue when a shorter window is available.
```

三条读法：

1. **总和是唯一对读者成立的数字。** 明细放括号里，是因为读者要决定的是"我要不要现在等"，
   不是"哪个域更慢"；后者只是前者的解释。
2. **这个电荷是可付的。** 少跑几轮就清了，所以它可以让 `--strict` 变红；第 126 条的按域欠账
   不是——没有人能付的账只能印，不能收。
3. **不会发生的等待永远不收费。** 队里没有可付的轮时，`budget` 说"没有可花的"，闸门即使在
   `--max-minutes 1` 下也退出 0：收一个不会发生的等待的钱，只会训练读者忽略这条规则。

**边界 134：一条队的代价是它所付各域之和，不是一个单价乘以轮数。** 每个域按自己的轮定价；
只有从没给自己计过时的域，才退回全队中位数——退回要说清楚，而不是让一个域悄悄借用别人的速度。

**边界 135：一个被要求接受的等待必须能被拒绝，而且用来拒绝的必须读者有的命令。**
`--max-minutes` 给出的解法只能是"少跑几轮"或"等一个更短的窗口"；如果它给出的解法是改一条
读者改不了的东西，那它就成了又一个永远红的闸门。

演示根的坑（第六十一轮已经踩过一次）：两个演示流文件的几何完全一样时，每条记录都会同时匹配两个，
于是 `ready 0`、"没有可付的"——看起来像产品缺陷，其实是自己造的并列。另外，一条写下
`"stream_path": null` 却没有 `versions/vNNN.json` 的记录，既不算"有文件"也不算"还在决定什么"
（写了仍在页面上的版本，或还背着一个拒绝），`_unrecorded_matters` 不会列它：演示记录要按
`persist` 真正写出的形状来造。


### 3.70 一次不是全部：给"付清"单独算一次价，让每个域自己的等待有上限

第 62 轮把等待按域算了，但句子回答的是"**这一次**调用要多久"。读者要决定的是"我现在要不要开始"，
而这件事取决于两个数：这一下多久，全部付清多久。只给一个，读者就得自己乘——乘出来的数错了，
他会在中途发现，那时他已经在等了。

同时，`--max-minutes` 还是一个数。一条队付 SMD 和 RCAEval，等待不是同一种：四点五分钟和六分钟，
不是"十分钟半这里可以、那里不行"。所以要能说"这个域最多四分钟，其他随意"。

两处改动：

| 位置 | 改了什么 |
| --- | --- |
| `_queue_budget` | 多两个字段：`drain_minutes`（把整个可付队列按域加总）和 `runs`（按这一下的轮数要跑几次才清空，`-(-ready // rounds)`，不引 math） |
| `budget_text` | `runs > 1` 时追加"；draining it would take N such run(s) and about X minute(s)"——只有分几次付时才说，因为全付时它就是这一下 |
| `_minute_limits` | `--max-minutes` 变成可重复：裸数字是全局上限，`domain=minutes`（或 `default=minutes`）只覆盖该域；域按 slug 比较，所以 `rcaeval-re1-ob=4` 认得出 `RCAEval RE1-OB` |
| `_strict_exit` | 逐域判上限：每个域用自己的上限，没有就继承全局；谁超了就只点谁的名，输出 `… on <domain>, above the X asked for:`；没有任何逐域数字时才退回全局那一句 |

页面（`var/budget61`，SMD 的轮 4.5 分钟、RCAEval 6 分钟，可付两轮）：

```
budget            brian evolve --rerun-unrecorded --rerun-limit 1 pays 1 round(s) (RCAEval RE1-OB); the queue falls 67% -> 33% of 3 round(s), about 6 minute(s); draining it would take 2 such run(s) and about 10.5 minute(s)
```

逐域上限（同一页面，`--strict --rerun-limit 0`）：

```
$ … --max-minutes "default=10" --max-minutes "rcaeval-re1-ob=4"
the next queue run would spend about 6.0 minute(s) on RCAEval RE1-OB, above the 4.0 asked for:

$ … --max-minutes "SMD=4"
the next queue run would spend about 4.5 minute(s) on SMD, above the 4.0 asked for:

$ … --max-minutes "5x"
--max-minutes wants a number or domain=minutes, got '5x'        (exit 2)
```

三条读法：

1. **两个问题要两个数。** "这一下多久"和"付清多久"不能互相代替；分几次付时第二个才是读者真正在权衡的，
   所以它必须由同一个函数算出来，而不是读者用轮数去乘一个中位数。
2. **逐域上限不是打折，是点名。** 它说的是"我不接受这个域的这段等待"，于是超出时点名的也必须是那个域；
   一个只说"总共超了"的闸门，会让读者以为要减的是另一个域。
3. **读不懂的上限必须报错，不能变成豁免。** 解析失败若被忽略，页面会继续声称一个没人检查的上限——
   那比没有上限更坏，因为它把"我设了限"写进了读者的信心里。

**边界 136：预算必须同时给"这一次"和"付清"的代价。** 只给前者，读者要自己乘；乘错的代价在他已经开始等之后才出现。

**边界 137：逐域的上限要在它点名的那个域上收费，没点名的域继承全局，绝不求和。** 而且这条参数读不懂时是错误，
不是豁免——一个不生效的上限比没有上限更坏。


### 3.71 等待上限属于那条流，不属于这个 shell

第 63 轮让 `--max-minutes` 可以按域下：`--max-minutes "rcaeval-re1-ob=4"`。问题是它只活在一次调用里。
同一个读的人明天要重新敲一遍；更糟的是同一个数字在两天里可能指两件不同的事 —— 今天"SMD 最多等 4 分钟"，
明天 `default=4` 顺手也管住了 RCAEval。一个上限是关于"这条流可以让我等多久"的陈述，这类陈述该和那条流的
配置住在一起，而不是住在某个终端的滚动缓冲里。

所以第 64 轮把上限搬进了部署状态本身：

```
$ brian evolve --set-domain-limit "rcaeval-re1-ob=4"
recorded          rcaeval-re1-ob: at most 4 minute(s) for one queue run
                  var\evolve\domains\rcaeval-re1-ob\current.json
```

写进去的是那个域自己的 `current.json`，和"哪些变量在生效、各自来自哪个版本"同一个文件：

```json
{
  "env": {},
  "sources": {},
  "updated_at": "2026-11-18T05:28:33+00:00",
  "budget": {
    "max_minutes": 4.0
  },
  "domain": "rcaeval-re1-ob",
  "source": "RCAEval RE1-OB"
}
```

之后**不带任何参数**的一次检查就会按它收费，只收超出的那一个域：

```
$ brian evolve --campaign --strict --rerun-limit 0
the next queue run would spend about 6.0 minute(s) on RCAEval RE1-OB, above the 4.0 asked for:
lower the budget with --rerun-limit, or re-run the queue when a shorter window is available.
```

`--ignore-state-limits` 则明确说"今天不按记录收费，只按命令行"，于是同一条命令退出 0。这是有意的：
记录里的上限是策略，命令行上的上限是今天的决定，两者都可以存在，但必须有一句话说谁赢。

版本页也会把这条策略念出来，否则一个没人看得见的上限就是没人能复核的上限：

```
domain           rcaeval-re1-ob  (RCAEval RE1-OB)
budget           at most 4 minute(s) for one queue run (from the deployment state)
```

写这条策略的过程本身露出两个必须处理的地方：

1. **推进一个版本不能顺手删掉同文件里的策略。** `_write_state` 是所有写入者（`apply_promotion`、
   `revert_promotion`、`set_state_budget`）的收口，它原本只写 `env`/`sources`/`updated_at`；
   如果不改，一次 `--verify` 成功之后的推进就会把 `budget` 抹掉，而页面看上去一切正常。
   现在它在调用方没有给 `budget` 时从磁盘把这一段读回来 —— 同一个文件里的两件事各自负责各自的键。
2. **写的域要用账本里的名字，不能用拼写。** 第一次运行写进去的是 `"source": "rcaeval-re1-ob"`，
   而 `promotions_for_domain` 和活动页都拿 `source` 和账本里的名字比；一个 slug 会让这个域看起来像一条
   没人测过的流。现在 `--set-domain-limit smd=3` 会先在账本里把 slug 解析成数据集名（`SMD`），
   解析不到才原样记下 —— 宁可记下读者写的名字，也不静默改名。

三条读法：

1. **上限是部署的一部分，所以它要能跟着部署走。** 一次调用的上限只保护那一次；跨天的上限才是一份承诺。
2. **同一个文件里的两类事实必须互不覆盖。** 配置由版本拥有，预算由读者拥有；写入者只碰自己那几个键，
   其余的从磁盘读回来。
3. **能被记录的东西必须能被看见。** 记录里有一条上限，页面就要有一行；否则读者会以为自己在一个
   没人告诉过他的天花板下面工作。

**边界 138：一条关于"等多久"的策略必须记在它约束的那条流的部署状态里，而不是某一次调用的命令行上 ——
一次调用的上限只约束那一次，跨天的上限才约束行为。**

**边界 139：同一个文件里的两件事必须各自负责各自的键；一个写入者删掉它不理解的块，是一次看不见的
破坏 —— 页面照旧，而策略已经没了。**


### 3.72 一次等多久，和排空要多久，是两句承诺

第 64 轮把"一次队列运行可以为这条流等多久"记进了那条流的部署状态。但那只是**一次**的价格。
"排空这条流的欠账要多久"是另一个数、另一句承诺：只收紧前者的读者，会在队列跑到一半时才发现后者，
而那时钱已经花了。

所以第 65 轮在同一个 `budget` 块里记第二个数：

```
$ brian evolve --set-domain-limit "rcaeval-re1-ob=10" --set-domain-drain "rcaeval-re1-ob=3"
recorded          rcaeval-re1-ob: at most 10 minute(s) for one queue run
recorded          rcaeval-re1-ob: at most 3 minute(s) to empty the queue
```

```json
"budget": {
  "max_minutes": 10.0,
  "drain_minutes": 3.0
}
```

两个写入各自只碰自己那个键：设排空上限不会顺手清掉昨天记的单次上限（边界 139 在一个更小的地方）。
检查时两句承诺分别收费，因此"单次没问题、排空超了"能单独成为失败：

```
$ brian evolve --campaign --strict --rerun-limit 0
emptying the queue would spend about 6.0 minute(s) on RCAEval RE1-OB, above the 3.0 asked for:
raise the ceiling, or drain the queue over several runs with --rerun-limit.
```

版本页把两句都念出来：

```
budget           at most 10 minute(s) for one queue run (from the deployment state)
drain            at most 3 minute(s) to empty the queue (from the deployment state)
```

**报告必须说自己按什么判的。** `--campaign --json-out` 现在多一个 `limits` 块，每个上限都带来源：

```json
"limits": {
  "max_minutes_by_domain": {"rcaeval-re1-ob": 10.0},
  "max_minutes_origin": {"rcaeval-re1-ob": "state"},
  "max_drain_by_domain": {"rcaeval-re1-ob": 3.0},
  "max_drain_origin": {"rcaeval-re1-ob": "state"}
}
```

这条是写的过程中发现的缺陷：第一版按"这个域在状态里有没有数"来判断来源，于是命令行上传了
`--max-drain-minutes "rcaeval-re1-ob=100"`（覆盖了记录里的 3）之后，报告仍然写着 `"state"`。
一个**碰巧和策略同值**的命令行决定，会被读成"没有人覆盖任何东西"。现在来源按"这次有没有给这个域传旗标"
判断，旗标覆盖即记 `"flag"` —— 即使两个数一样。

三条读法：

1. **等待有两个价格，两个都要能记录、能收费。** 只记一个，读者就会在另一个上超支。
2. **文件里的两个承诺要各自归位。** 写一个不删另一个，这是第 64 轮的边界在一个更小的位置上重演。
3. **报告里的每个上限都要有出处。** 政策是明天的、命令行是今天的；分不清这两者的报告没法复核。

**边界 140：一次等待有两个价格 —— 一次运行、排空全程 —— 两句都要能记录、能被闸门单独收费；
只收紧其中一个的读者，会在另一个上超支。**

**边界 141：报告必须说明它按哪些上限判定，以及每个上限来自常驻策略还是本次命令行；两个数同值不等于
没人覆盖，出处只能由"这次有没有传旗标"回答。**


### 3.73 页面要念出它按什么上限判定，上限也要能被撤销

3.72 把两个上限（一轮、整队）写进了部署状态，并把它们送进了 `--json-out`。但一个人读的是正文：
一页没有任何"上限"字样的报告，读起来和"根本没设上限"一模一样——而这两件事的结论不同
（前者是被判过了，后者是没人判）。同一轮里还有一个更笨的缺口：`--set-domain-limit` /
`--set-domain-drain` 只能写入，不能撤销。一条只能加不能减的政策，迟早会变成一条没人记得为什么存在的政策。

于是正文多了一行，写法与数字一起来源：

```
$ brian evolve --campaign --rerun-limit 0
budget            brian evolve --rerun-unrecorded --rerun-limit 2 pays 2 round(s) (RCAEval RE1-OB, SMD); the queue falls 67% -> 0% of 3 round(s), about 10 minute(s) (RCAEval RE1-OB 6, SMD 4.5)
limits            one run 10.0 minute(s) on rcaeval-re1-ob (state); a drain of 3.0 minute(s) on rcaeval-re1-ob (state)
```

来源要跟着数字走，因为同样的数字有两种来路。用命令行盖过一次，同一行就读出 `(command line)`：

```
$ brian evolve --campaign --rerun-limit 0 --max-drain-minutes "rcaeval-re1-ob=100"
limits            one run 10.0 minute(s) on rcaeval-re1-ob (state); a drain of 100.0 minute(s) on rcaeval-re1-ob (command line)
```

这一点是 3.72 那个缺陷的延续：3.72 的 `limits` 视图按"这个域在状态里有没有数字"来判断来源，
于是命令行把一个记录在案的 3 覆盖成 100 时，它仍然写 `state`——一个碰巧等于政策的命令行决定，
被报告成了"没人覆盖"。现在只要某个域上给了旗标，来源就是 `flag`，数值相等也不例外。

撤销走同一个语法，把分钟数换成 `none`：

```
$ brian evolve --set-domain-drain "rcaeval-re1-ob=none"
cleared           rcaeval-re1-ob: no drain ceiling
$ brian evolve --set-domain-limit "rcaeval-re1-ob=none"
cleared           rcaeval-re1-ob: no queue-run ceiling
```

撤掉最后一个上限之后，`current.json` 里的 `budget` 块整个消失，正文也不再打印 `limits` 行——
"没有上限"和"有上限"在页面上必须长得不一样，否则读者无法判断自己读到的结论是被判过的。

**这一轮真正跑出来的缺陷**：第一次撤销时命令打印了 `cleared`，文件里的值却还在。原因正是 3.71 的
那个"保护"——`_write_state` 在调用方没有传 `budget` 时去磁盘把它读回来，以免一次版本写入抹掉同一份
文件里的政策。但它用"传进来的块是空的"来判断"调用方没说"，而撤销恰好就是"传进来一个空块"。于是
撤销被立刻还原：页面说撤了，下次读还是 10 分钟。修法是把两种沉默分开：

```python
budget = dict(state.get("budget") or {})
if not budget and "budget" not in state:   # 只有"键都没传"才去磁盘读
    ...                                     # 显式的 {} 是撤销，读回来等于复活
```

三个读数：

1. **判定过就必须念出来。** 一页报告的价值取决于读者能重建它的判定条件；上限只存在于 JSON 里，
   等于要求读者先猜有没有、再去查。
2. **来源不是装饰。** `(state)` 与 `(command line)` 的区别决定了下次该改哪里：一个是改政策，
   一个是改这条命令。相等的数值不能证明没被覆盖过。
3. **能写就要能撤，而且撤销要能和"没说话"区分开。** 缺键、空字典、显式清除在代码里是三种状态，
   一个用"空不空"判断"说没说话"的实现会把第三种当成第一种。

**边界 142：一页按上限判过的报告必须念出那个上限；"没有上限"与"上限在别处"不能长得一样。**
一份不念上限的报告让读者无法知道结论是被判过还是没人判——而这恰好是读者最需要知道的事。

**边界 143：能写入的政策必须能撤销，撤销必须能与"写者无话可说"区分开；空的那一种就是区分本身。**
一条只能加不能减的政策会累积成没人记得来由的规则；而一个用"空"表示两种含义的写者，
会在撤销时把政策复活。

### 3.74 门禁也要念出它按什么判定，撤销要跟着它的命令走

3.73 让正文念出了上限，但**门禁**没有：`--audit --strict` 会因为"这一队要跑 10 分钟，超过你要求的 5 分钟"
退出 1，而它的报告里没有任何"上限"字样。读者看到的是一个失败的检查，看不到它按哪个数判的——
两条读同一本账的路径，用不同的方式描述同一次判定（第 51 轮边界 108 的老问题，只是换了个位置）。

于是 `--audit` 在报告末尾打印同一句话，用的是同一个函数：

```
$ brian evolve --audit --rerun-limit 0
…
limits            one run 10.0 minute(s) on rcaeval-re1-ob (state); a drain of 3.0 minute(s) on rcaeval-re1-ob (state)
```

`--ignore-state-limits` 时这句话不出现——因为门禁确实没读它们，声称读过就是另一种撒谎。有测试把
"两条路径说出同一句话"钉住：`--audit` 与 `--campaign` 的 `limits` 行必须逐字相等。

机器可读的一侧也补上了：`--audit --json-out` 现在写出 `limits`、`limits_text`，以及它已经打印过的
全部计数（`moved`、`relabelled`、`rounds`、`trials`、`retired`、`unreconstructable`、`inconsistent`、
`rules_hash`、`entries`）。一份检查如果只在终端里说明判据，事后就没有可复核的记录（边界 131）。

第二半是撤销。3.73 让上限能被撤销，但撤销只活在那条命令的终端里：状态文件里键被删掉，
"撤过"和"从没设过"在文件里长得一模一样。这个信息不能塞回状态（那会让空政策复活，正是 3.73 的缺陷），
所以由做出决定的命令自己带出来：

```
$ brian evolve --set-domain-drain "rcaeval-re1-ob=none" --json-out var\write67.json
cleared           rcaeval-re1-ob: no drain ceiling

{
  "recorded": [],
  "cleared": [{"domain": "rcaeval-re1-ob", "kind": "drain"}],
  "in_force": {"run": {"rcaeval-re1-ob": 10.0}, "drain": {}},
  "files": ["var\\evolve\\domains\\rcaeval-re1-ob\\current.json"]
}
```

`recorded` 与 `cleared` 是同一种形状的两半（`domain` / `kind` / `minutes`），`in_force` 是写完之后
真正在生效的集合——这样"我说了什么"和"现在是什么"在同一次输出里，读者不必自己把两个来源对上。

三个读数：

1. **判定的依据属于判定本身。** 门禁、正文、JSON 是三处入口，一处念了另外两处不念，就等于
   把"可复核"这件事交给读者碰运气。
2. **撤销是一种状态，不是一次沉默。** 状态文件用"键不存在"表示"没有上限"，撤销必须由命令说出
   自己做了什么；把撤销写回状态会让它复活，所以它属于那条命令的输出。
3. **`recorded` 与 `cleared` 要同形。** 两个形状不同的清单，读者无法回答"这个域现在到底被什么限制"。

**边界 144：一条会通过退出码作出判定的报告，必须在同一份输出里念出判据；只在终端里念，
事后就没有可复核的记录。**

**边界 145：撤销必须由做出撤销的那条命令记录，而不是由状态文件的空缺推断——
空政策既表示"从没设过"也表示"撤过了"，两者不能靠猜。**

### 3.75 上限不在规则书里，所以规则审计看不见它移动

3.74 让门禁念出了它读到的上限。可是**每一轮记录**里没有上限：`EvolveRules` 决定"什么叫更好"，
等待上限决定"这一队今天能不能跑"，后者存在部署状态里，于是它可以在一条规则都不改的情况下被收紧——
同样的测量、同样的规则指纹、不同的判决，而 `--audit`（唯一被造出来发现"存档判决与今天的规则不一致"的地方）
对它一无所知。

这一轮把上限写进记录，并让门禁做一次比较：

```
$ brian evolve --audit --rerun-limit 0
recorded rounds   2  (0 candidate measurements re-decided, no re-run)
every recorded verdict survives today's rules.
limits            one run 4.0 minute(s) on smd (state)
ceilings          1 of 2 round(s) were judged under ceilings that differ from today's (round-old-limit.json); the rules did not change, the limits did; 1 round(s) do not say which ceilings they were judged under
```

比较**只比数字，不比来源**：`(state)` 与 `(command line)` 回答的是"谁决定的"，不是"限制是多少"——
按十分钟判过的一轮，无论十分钟是从政策还是从命令行来，判的都是十分钟。有测试把这一点单独钉住。

"没说"单独计数，绝不并入任何一边：把没有上限字段的旧记录算成"一致"，等于宣称历史是在今天的上限下判的；
算成"不一致"，等于给每条旧记录记一笔它没犯的错。这正是边界 123 在另一个位置的同一条规矩。

`--rerun-unrecorded` 的开头也念同一句（它此前只念 `budget`）：

```
candidates        2 of 2 round(s) have exactly one file; 0 cannot be placed
progress          0 round(s) already re-measured into a named successor; 2 still to do
budget            brian evolve --rerun-unrecorded --rerun-limit 2 pays 2 round(s) (RCAEval RE1-OB, SMD); …
limits            one run 10.0 minute(s) on rcaeval-re1-ob (state); a drain of 20.0 minute(s) on rcaeval-re1-ob (state)
stops             at the first round that cannot be re-run; the rest keep their place
```

"该在变化时失败"落成一个可选的闸门，而不是默认失败：

```
$ brian evolve --audit --strict --rerun-limit 0 --max-limit-drift 0
1 recorded round(s) were judged under ceilings that differ from today's, above the 0 asked for:
their verdicts were reached under other limits, so --strict exits 1 until they are re-decided
with the ceilings in force now, or the cap is raised.
```

默认只是警告，理由与 3.63 相同：这个仓库里所有旧记录都写在上限字段存在之前，默认失败会让门禁永久变红——
一条永远清不掉的检查等于没有检查。把 `--max-limit-drift` 用在别的模式上会被拒绝并说明原因，而不是静默失效
（第 40 轮"某个模式里悄悄不生效的旗标"那条教训，换了个位置）。

三个读数：

1. **判定条件也是判定的一部分。** 规则指纹能证明"按什么规则判的"，证明不了"按什么代价判的"；
   后者住在部署状态里，所以必须被记录，否则一条状态改动会伪装成一次规则变化。
2. **比较要比该比的东西。** 来源不参与比较，因为来源解释决定，不解释数额。
3. **"没说"是第三种状态。** 把它并进任何一边都是在替历史回答一个它没回答的问题。

**边界 146：判决所依据的等待上限必须写进那一轮的记录；否则收紧一次上限，会被读成规则变了。**

**边界 147：比较"记录"与"今天"时，"没说"必须单独计数，不能并入一致或不一致——
把沉默算成任何一边，都是在替历史回答它没回答的问题。**

### 3.76 页面也要报同一列：默认只在记录够新时才亮

3.75 让 `--audit` 比较"这一轮按什么上限判的"，并把上限写进记录。可是**页面**（`--campaign`，
读者真正会看的那一页）只念一句 `limits`，不报有多少轮的上限已经变了；而 `--max-limit-drift`
只在 `--audit` 里生效，等于把唯一会看这条的读者挡在门外。

这一轮把同一列搬到每一行：

```
$ brian evolve --campaign
rounds            2  (0 candidate measurements, 0 accepted, 0 rejected)
limits            one run 4.0 minute(s) on smd (state)
ceilings          1 round(s) were judged under ceilings that differ from today's: the rules did not change, the limits did

SMD                     2      0      0 -         no claim recorded
  ceilings          1 of 2 round(s) were judged under ceilings that differ from today's; 1 round(s) do not say which ceilings they were judged under
```

真实账本（56 轮，几乎全部写在上限字段之前）只印出后半句——这是对的：

```
  ceilings          38 round(s) do not say which ceilings they were judged under
```

"没说"不冒充"一致"，也不冒充"不一致"（边界 147 在页面上的同一条规矩）。

**默认只在记录够新时才亮。** `--max-limit-drift` 现在两个模式都认；而它的**默认值**不是 0，是
"当所有存活记录都写了上限时才是 0"：

```
$ brian evolve --campaign --strict          # 还有一轮没写上限
$ brian evolve --campaign --strict          # 那一轮被退役后
1 recorded round(s) were judged under ceilings that differ from today's, above the 0 asked for:
their verdicts were reached under other limits, so --strict exits 1 until they are re-decided
with the ceilings in force now, or the cap is raised.
```

这条规则的理由是第 33 轮那条：**一条清不掉的检查等于没有检查**。默认失败在旧记录还在的时候会让门禁
永久为红，而唯一能清掉它的动作（重跑或退役那一轮）跟"上限变了"无关。

**运行时发现的缺陷**：页面按**数据集名**（`SMD`）分行，上限视图按**部署状态的 slug**（`smd`）存键，
第一版直接比字符串，于是每一行都"没有可比的轮"，漂移恒为 0——一个只在合成数据上才会暴露的空比较。
修法与 3.52 节那次同源：比较身份（`domain_slug`），不比拼写。

三个读数：

1. **同一列要在两个页面上都出现。** 读者不会为了看"上限有没有变"去另一个模式再跑一次。
2. **默认值是一条策略。** "默认 0"听起来严格，实际会让旧账把门禁按红；只有清得掉的时候，严格才是要求。
3. **身份不是拼写。** 行名和键名来自两个不同的命名空间，等号在这里永远为假。

**边界 148：同一份事实在两个地方各有名字时，要按它们的共同身份比较，不能按任一边的拼写。**

**边界 149：默认闸门只在它能被清掉的时候才亮；在历史让任何命令都清不掉的时候，同一条发现只能是警告。**

### 3.77 账本的每一行都要说出它按什么条件判定

`--audit` 与 `--campaign` 已经会回答"有多少轮是在另一套上限下判的"，但它们都没有回答一个更小的问题：
**这一轮当时被允许等多久？** `--history` 列出了每一轮的承诺、文件和判决，唯独没有列出判定条件，于是两段
中间改过策略的账读起来像一次连续的实验。

```
$ brian evolve --history
ceilings          1 of 2 round(s) record the wait ceilings they were judged under; 1 do not
 1  …  [no file recorded]
      ceilings  one run 10.0 minute(s) on smd  (differs from today's)
 2  …  [no file recorded]
```

三处细节是有意的：

- **没记录的轮不印行，但表头必须报数量。** 一列通常为空时必须自己说明这一点，否则它的沉默会被读成"没有
  设过上限"（边界 142），而这个列表看上去会比实际更统一。真实账本上是 `0 of 74 round(s)`——全部写在上限
  字段存在之前。
- **每行的旁注用的是与 `--campaign` 表头同一个比较**（`limit_drift([record], today)`），所以两页不可能
  互相矛盾（边界 108 的同一条要求）。
- **"全局上限"与"某个域的上限"不是同一个承诺。** 前者同时约束这一轮没碰过的域，所以即使数值相同也报
  `differs`；这一点有专门的测试固定，免得以后有人按"数值相等"把它们合并。

## 默认闸门什么时候才敢亮

默认值（不给 `--max-limit-drift`）只在**所有存活记录都写了上限**时才是 0，否则只是警告（第 69 轮）。
`limit_drift` 本来就只看存活记录，所以退役/重跑之后它会自己回到 0——这是可以现场演示的：

```
$ brian evolve --campaign --strict --rerun-limit 0      # 一轮没写上限
exit=0
$ brian evolve --retire round-before-limits.json --re-measured-as round-old-limit.json
retired           round-before-limits.json  (re-measured as round-old-limit.json)
$ brian evolve --campaign --strict --rerun-limit 0
exit=1
ceilings          1 round(s) were judged under ceilings that differ from today's: the rules did not change,
                  the limits did
1 recorded round(s) were judged under ceilings that differ from today's, above the 0 asked for:
```

不放旗标，退出码自己从 0 变成 1。但这也暴露了另一个事实：**这本账（74 轮）上默认闸门永远亮不起来**，因为
没有任何命令能给旧记录补写上限于事后——重跑会写一条新记录并退役旧的，而队列里 37 轮连候选定义都没有。所以
诚实的报告是 `0 of 74` 加一条警告，而不是一句"检查通过"。

**边界 150：** 账本列出每一轮的承诺、文件与判决时，也必须列出它的判定条件；否则两段中间改过策略的账会被当作
一次连续的实验来读。

**边界 151：** 一个默认闸门若依赖账本当前并不满足的前提，页面必须说出这一点；否则"检查通过"实际意味着
"检查根本没开"。


### 3.78 欠账要按域报，而且要说它能不能被付掉

第 70 轮把每一轮的上限印到了 `--history` 的行上，表头也报了总数。但"38 轮没说"这句话只回答了"有多少"，
没回答**哪一流欠的**，也没回答**有没有命令能让这个数字变小**。两句话补上之后，一列警告才变成一次决定。

```
$ brian evolve --campaign
ceiling debt      56 of 56 round(s) do not record the ceilings they were judged under, and none of them can
                  be re-run, so the default --max-limit-drift cap stays a warning
```

```
$ brian evolve --history
ceilings          1 of 3 round(s) record the wait ceilings they were judged under; 2 do not
  ceiling debt    SMD              1 of 2 round(s) do not record them
  ceiling debt    RCAEval RE1-OB   1 of 1 round(s) do not record them
```

`--unrecorded` 的队列表里，会写上上限的那些轮被标出来：

```
SMD   round-ceilings-missing.json   data/smd/stream-one.json   wrote 1 version(s) still on the page; 1 refusal(s) still carried  [+ceilings]
```

付掉之后，同一句就变成"会亮"：

```
$ brian evolve --campaign --rerun-limit 1          # 一轮缺上限、且可重跑
ceiling debt      1 of 1 round(s) do not record the ceilings they were judged under; re-running 1 of them
                  would leave none, so the default --max-limit-drift cap would arm
$ brian evolve --rerun-unrecorded --rerun-limit 1
paid              SMD x1
$ brian evolve --campaign                          # 债已还，这行不再出现
```

三件事是有意的：

- **payable 只数队列真能重跑的轮**（有候选定义、几何唯一、还决定着什么），因为一次重跑会写一条**新**记录
  并把旧的退役；不能重跑的轮永远留在"没说"里，把它们的数算进 payable 就是承诺一个不会发生的动作。
- **余数为 0 才说"会亮"**，否则明说还剩多少：`would leave 3 without one, so … stays a warning`。这与第 69 轮
  的默认闸门规则是同一条——闸门只在能被清掉的时候才亮。
- **标记打在每一行上而不是汇总**，因为读者要决定跑哪几行，不是跑多少行。

## 跑出来的一个崩溃

`--history` 在演示账本上直接抛了 `TypeError: can only join an iterable`：账本行里的 `accepted` 是**计数**，
轮文件里是**名字列表**，合并后留下的数字被 `",".join(accepted)` 撞上。真实账本的记录两者都是列表，所以从
没暴露。修法是页面先判断形状、印 `accepted=-` 而不是崩：**历史页是读者在出问题时去看的地方**，它不能因为
一条记录的形状而退出（边界 153）。

**边界 152：** 一个按整本账统计的欠账也必须按流统计，并说明有没有命令能让它变小；只有"38 轮没说"而没有
"其中 0 轮可重跑"时，这一列会教读者忽略它。

**边界 153：** 页面必须能读形状更老的记录；历史页尤其如此——它是出问题时去看的地方，应当印出能印的，
而不是抛异常。


### 3.79 能付的欠账才进闸门：上限欠账与"队列够不着"的那一半

3.78 让页面按域报出"多少轮没记过判它们所用的上限"，并说明重跑队列能不能把它清零。那个数字仍然是警告：
没有任何退出码读它。一个域 100% 的轮都没记上限，`--strict` 照样 0。

3.79 把它做成可以要求的：`--max-ceiling-debt SHARE`，但只在一个条件下允许点亮——重跑队列能覆盖**每一个**
缺上限的轮。两条现成的规则在这里再次适用：

- 能付的账才收费（126/135）：收一个谁也降不下来的比例，等于把这道闸门永久判红，没人会再看它；
- 默认只在记录够新时才亮（149）：真实账本里 74 轮全部早于该字段，默认点亮就是全红。

```
$ brian evolve --campaign --strict --max-ceiling-debt 0.5 --rerun-limit 0     # var/ceil72p（可付）
ceiling debt      1 of 1 round(s) do not record the ceilings they were judged under; re-running 1 of them
                  would leave none, so the default --max-limit-drift cap would arm
1 of 1 round(s) do not record the ceilings they were judged under (100%), above the 50% asked for:
re-running the queue would leave none, so --strict exits 1 until it is paid.
                                                                              # 退出码 1

$ brian evolve --audit --strict --max-ceiling-debt 0.5 --rerun-limit 0        # 同一账本，审计给同一句
1 of 1 round(s) do not record the ceilings they were judged under (100%), above the 50% asked for:
re-running the queue would leave none, so --strict exits 1 until it is paid.
run `brian evolve --rerun-unrecorded` to measure them again with their ceilings.

$ brian evolve --campaign --strict --max-ceiling-debt 0.5 --rerun-limit 0     # var/ceil72（一半够不着）
ceiling debt      2 of 2 round(s) do not record the ceilings they were judged under; re-running 1 of them
                  would leave 1 without one, so the default --max-limit-drift cap stays a warning
2 of 2 round(s) do not record the ceilings they were judged under (100%), above the 50% asked for:
only 1 of them can be re-run, so --strict does not charge it: a gate that can never be cleared is a gate
nobody runs.
                                                                              # 退出码 0

$ brian evolve --rerun-unrecorded --rerun-limit 1                             # 付掉
paid              SMD x1
$ brian evolve --audit --strict --max-ceiling-debt 0.5 --rerun-limit 0
ceilings          every recorded round was judged under today's ceilings     # 退出码 0
```

**一个只有跑起来才看得见的缺口：命了文件名的轮，队列够不着。** `_unrecorded_matters` 只列"文件不知"的轮
（3.53 之后队列按这个集合工作）。于是两类缺上限的轮并不同命：

- 没记文件、但几何只匹配一个文件 → 队列能重跑，重跑后新记录带上限 → 可付；
- 记了文件、但没记上限 → 队列**从不**碰它，所以既不能被重跑，也不能被收费 —— 它只出现在页面那行文字里。

这正是收费条件必须写成"payable 覆盖全部缺失"而不是"有一个可付就算"的原因：否则一半能付、一半永远付不了
的账本，会得到一道永远红的闸门（126 的原话）。

**第三种状态：判的时候根本没有上限。** 一轮若在上限为空的情况下被判定，它记下的是 `{}`，而 `{}` 在
`ceiling_shortfall` 里和"没说"一样算缺失。这个处理是对的（读者确实无法知道它被什么限制），但它说明"重跑
能把欠账清零"只在这些轮重跑时**也**带着上限才成立。所以演示根必须先 `--set-domain-limit`，否则重跑写出的
新记录仍然是 `{}`，欠账原样保留 —— 队列付了一轮，`ceiling debt` 却一行没少。这条在 `var/ceil72_demo.py`
的注释里。

**JSON 同形（131 的延伸）。** `--history --json-out` 现在输出每轮的上限原文与"是否与今天一致"，以及整个
账本的 `{missing, rounds, payable, by_domain}`；`--unrecorded --json-out` 也带上同一个块，并多给一个
`after`（重跑之后还剩几个）—— 读者原本要在两条文本行之间做减法。

实测（`var/ceil72` 两轮、payable 1）：

```
{"ceilings": {"missing": 2, "rounds": 2, "payable": 1,
              "by_domain": [{"domain": "SMD", "rounds": 2, "missing": 2, "share": 1.0}]},
 "rounds": [{"round": "round-payable.json", "ceilings": null, "ceilings_text": "no ceilings recorded"},
            {"round": "round-named.json",   "ceilings": null, "ceilings_text": "no ceilings recorded"}]}
```

**边界 154：欠账只有在某条命令能把它降下来时才允许进闸门收费。** 收一个没有执行者的比例，读者学到的是
"这道闸门可以忽略"，而不是"这件事要修"。

**边界 155：`{}`（判的时候没有上限）与缺失（没说）必须在记录里分得开，并且在收费时被同等对待。** 区别在
于付费方式：前者重跑之后仍然是 `{}`，所以它不进 payable；后者重跑一次就补上了。

## 3.80 队列够不着的那一半：命了文件的轮也能被重跑

第 71 轮把两种"缺上限"的轮数合进了一列：文件未知的，和命了文件却从没记录过判定上限的。第 72 轮只给
第一种上了闸门，第二种被明确地判为"不收"，理由是一条命令能把它变小的欠账才配失败。那个判断在当时的代码里
是对的，但它同时暴露了事实的另一半：**对第二种，没有任何命令能把它变小**。队列的重跑按"文件未知"来选轮，
于是这些轮永远既不会被重测，也不会被收费 —— 它们只是被数着。

这一轮给队列补上第二条路：`--with-ceilings`，把"命了文件、没记上限、而且今天还在决定什么"的轮用记录里
自己的候选和文件重测一遍，写回上限。于是闸门对它们也开始收费，这时的收费是被允许的 —— 它能被付清。

```
$ brian evolve --rerun-unrecorded --with-ceilings --rerun-limit 0      （两种族各一条轮）
candidates        1 of 1 round(s) have exactly one file; 0 cannot be placed
ceilings          1 round(s) name their file and never recorded their ceilings; re-measuring them here
---- re-running round-payable.json on data/smd/stream-one.json
recorded round var\evolve\round-20261118T061536Z.json
retired round-payable.json (re-measured as round-20261118T061536Z.json)
---- re-running round-named.json on data/smd/stream-one.json
recorded round var\evolve\round-20261118T061536Z-2.json
retired round-named.json (re-measured as round-20261118T061536Z-2.json)
  ceilings         the successor records the ceilings it was judged under
ceilings          1 of 1 re-measured with their ceilings; 0 still without
```

第二条路的欠账现在能进闸门了：

```
$ brian evolve --campaign --strict --max-ceiling-debt 0.5 --rerun-limit 0   （只剩命了文件的那条轮）
ceiling debt      1 of 1 round(s) do not record the ceilings they were judged under; re-running 1 of them would leave none, so the default --max-limit-drift cap would arm
1 of 1 round(s) do not record the ceilings they were judged under (100%), above the 50% asked for:
re-running the queue would leave none, so --strict exits 1 until it is paid.
$ brian evolve --rerun-unrecorded --with-ceilings --rerun-limit 1
ceilings          1 of 1 re-measured with their ceilings; 0 still without
$ brian evolve --campaign --strict --max-ceiling-debt 0.5      → 退出 0，没有 ceiling debt 行
```

### 运行才暴露的两个缺陷

**一、同一秒内的两条记录是同一条。** 第一次跑双族重测时，两条轮写成了**同一个** `round-20261118T061510Z.json`：
`persist` 用 `started_at` 到秒来命名记录，而队列一秒内能跑完两条短轮。后一条覆盖前一条，于是前一条轮被"退役"
进了一个内容其实是第二条测量结果的后继。这不是显示问题 —— 一份记录是内容，不是时间戳。修法是 `persist` 在
目标文件已存在时加后缀（`-2`、`-3`……），代价是零，因为下游一律按名字找文件，从不解析名字的形状。

**二、"够不着"和"不值得"在页面上长得一样。** 第 71 轮那句 "N of M round(s) do not record the ceilings they
were judged under, and none of them can be re-run" 把两件事压成了一件。现在页面会说
"N of them name their file, so --with-ceilings can re-measure them"，而这句只在真有时才出现。

**边界 156：只能付"文件未知"那一半的队列，等于留了一半够不着的欠账；够得着与否要跟着"缺的是什么"走，
而不是跟着一种缺法走。**

**边界 157：一条记录由内容标识，不由时间戳标识 —— 同一秒的两次测量是两条记录；后继文件被覆盖时，
"退役"退进的是错误的证据。**


## 3.81 能付的欠账要说出命令：排空时它也自动跟上

第 73 轮让队列够得着"命了文件、没记上限"的那一族，但页面上那句话仍然只是一个诊断：它说"重跑会把欠账清掉"，
却没说**跑什么**。与此同时 `--with-ceilings` 只有在读文档的人想起来的时候才会被敲 —— 一个要靠记得的开关，
等于一个默认关闭的开关。

这一轮做两件事：把那句话补成命令，并把"排空"定义成"付掉所有能付的"。

```
$ brian evolve --campaign --rerun-limit 0
ceiling debt      2 of 2 round(s) do not record the ceilings they were judged under; re-running 1 of them would
                  leave 1 without one, so the default --max-limit-drift cap stays a warning; 1 of them name their
                  file, so --with-ceilings can re-measure them; measure them with brian evolve --rerun-unrecorded
                  --with-ceilings --rerun-limit 0

$ brian evolve --rerun-unrecorded --rerun-limit 0            （排空，不带任何 --with-ceilings）
ceilings          1 round(s) name their file and never recorded their ceilings; re-measuring them here (a drain pays this family too)
---- re-running round-payable.json on data/smd/stream-one.json
recorded round var\evolve\round-20261118T062253Z.json
retired round-payable.json (re-measured as round-20261118T062253Z.json)
---- re-running round-named.json on data/smd/stream-one.json
recorded round var\evolve\round-20261118T062253Z-2.json
retired round-named.json (re-measured as round-20261118T062253Z-2.json)
  ceilings         the successor records the ceilings it was judged under
ceilings          1 of 1 re-measured with their ceilings; 0 still without
drained           2 round(s) retired in this invocation; 0 still in the queue; the unrecorded share 50% -> 0%
paid              SMD x2
```

规则是一句话：**排空（`--rerun-limit 0`）付掉所有能付的；单步（默认 1 轮）只付一族。** 单步不自动跟上，
是因为默认值一旦悄悄把自己花的时间翻倍，第 114 条边界（"花钱的命令默认只花一次"）就成了空话；
`--with-ceilings` 可以显式打开它，`--without-ceilings` 可以显式关掉它（排空时）。

### 运行才暴露的两个缺陷

**一、两个数被加成了一个数。** 第 73 轮把 `ceiling_named` 加进了 `ceiling_payable`（闸门需要一个"总共能付多少"），
然后又把这两个数分别传给页面 —— 而格式化函数里 `named = min(named, missing - payable)`，`payable` 已经含了它，
于是 `named` 被夹成 0：页面永远说不出"其中 N 条命了文件"。两个含义不同的数不能交给同一个参数。修法是分开：
`ceiling_file_payable` 给页面，`ceiling_payable`（并集）给闸门。

**二、排空总结少算了一条。** 第一次排空实际退役了 2 条轮，却打印 `drained 1 round(s) retired` 和 `paid SMD x1`
—— 计数只在"文件族"循环里加。同一份输出里"清掉了什么"和"还剩什么"必须回答同一个问题（边界 98 在一行之下）。

**边界 158：一句"这条欠账能付"必须带上付款命令；读者被告知会被收钱、却没被告知敲什么，拿到的还是诊断。**

**边界 159：排空意味着"付掉所有能付的"，单步意味着"付一族"；默认值的花费不许随着族数悄悄翻倍。**


### 3.82 预算要说钱，也要说族数：排空会开出第二族

第 74 轮让 `--rerun-limit 0` 默认付两族（文件族 + 上限族），却没有让价格知道这件事。于是同一页上：

```
budget            brian evolve --rerun-unrecorded --rerun-limit 1 pays 1 round(s) (SMD); the queue falls 50% -> 0% of 2 round(s), about 5 minute(s)
ceiling debt      2 of 2 round(s) do not record the ceilings they were judged under; ... 1 of them name their file, so --with-ceilings can re-measure them
```

上面这行报的是"一轮、约 5 分钟"，而推荐的那条命令会把两轮都量一遍。读者据以决定要不要开始的那个数字，是按一件更小的活算出来的。

改了三处，都落在"同一句话"上：

1. `_queue_budget(..., ceiling_limit=...)` 增加返回 `ceiling_rounds` 与 `families`，并把分钟数把第二族算进去——两族走的是同一个 `run_round`，一轮的时间是同一轮的时间；
2. `budget_text` 把族数写在钱的旁边，而不是折进钱里：`it also re-measures 1 round(s) that named their file but never recorded their ceilings, about 10 minute(s)`。读者挑两条命令时，需要看到的是"一条开了两族"，不是平均掉之后的分钟数；
3. 文件族为空、只剩上限族时，那句不再说 `nothing to spend`——它现在写 `... --with-ceilings measures 9 round(s) that named their file but never recorded their ceilings, leaving the unrecorded share unchanged at 66% of 56 round(s)`。一条正在花钱的命令不能顶着"没有可花的"这句话；
4. 命令串本身带上 `--with-ceilings`：排空默认会开这一族，但"默认"是这个构建的事实，读者复制的那一行里看不见它；
5. `--history` 在上限欠债行之后补 `measure` 行，用的是页面同一个 `_ceiling_command`：

```
  ceiling debt    SMD              38 of 38 round(s) do not record them
  measure         brian evolve --rerun-unrecorded --with-ceilings --rerun-limit 0
```

只有跑起来才发现的缺陷：`_queue_budget` 里 `0` 的意思是"把这一族排空"，而我把 `0` 当成"不碰这一族"传给了 `--without-ceilings` 的路径，于是那页仍然报着九轮的钱。`None` 才是"不碰"——一个参数的默认值若有两种"零"，两种都要在签名上写清楚。

**边界 160：一句给出价格的命令，必须把这条命令会开出的每一族都算进去。**只算一族的价钱不是保守估计，它是一张更小的活的价格表：读者照它决定开始，然后在一个比报价更长的等待里发现差额。

**边界 161：读者为诊断打开的那一页，必须和做出诊断的那一页指向同一个下一步。**`--history` 与 `--campaign` 若各自拼一句命令，两页迟早给出不同答案；同一个函数是唯一能保证它们一致的东西。


### 3.83 沉默不等于没有：被留下的那一族要说出来，命令要先报族数

第 75 轮让预算算上了第二族，但只在那族**被开**的时候。`--without-ceilings` 的页面于是和"根本没有上限欠账"印同一句话——什么都不说；`--rerun-unrecorded` 的队列头也只在真开那族时才提它。读者分不清"没有这笔账"和"这次不付"。

三处改动：

1. `_queue_budget` 无论这次开不开这族，都从记录里数出 `ceiling_pending`，并返回 `ceiling_pending` 与 `ceilings_skipped`；
2. `budget_text` 把留下的那族写成一句话，而不是靠沉默：`... it leaves 1 round(s) that named their file and never recorded their ceilings for another run, about 5 minute(s)`；文件族为空、同时又留下上限族时，那句不再说"没有可花的"：`nothing to spend on the file family: 9 round(s) named their file and never recorded their ceilings, and this command leaves them for another run (the share stays 66%)`；
3. 队列头新增一行，把"族数"和"每族的轮数"分开报：

```
families          2: 1 round(s) from the file queue, 1 from the ceiling queue
```

真实账本上是 `families 1: 0 round(s) from the file queue`——文件族这一轮本来就没有可付的，写着比省掉好：一个总是缺席的字段，读者只会以为它不存在。

三条读法：

- **沉默是一种断言**。页面不写某族，读者只能理解为该族没有欠账；要表达"这次不动"，必须有一句话。
- **族数不是轮数**。"两族各一轮"和"一族两轮"总轮数相同、等待相同，但它们是两种活：一种把两类账各推进一格，一种把一类账推进两格。
- **被留下的那族要有下一句**。页面已经给出 `measure` 行，命令头由这一轮补上（见第 64 项）。

**边界 162：没做的那部分也要说出来。**一句"没有"必须能区分"不存在"和"这次不动"；否则读者用一次被拒绝的检查误读整张表。

**边界 163：一次调用的形状是族数加每族的轮数，不是总轮数。**只报总数会把"两族各一轮"和"一族两轮"报成同一种活，而它们推的不是同一本账。


### 3.84 拒绝要带着补救走：命令头自己说出少做的那一族，记录写下这次的形状

第 76 轮让页面对被留下的上限族说了一句话，但队列命令自己的头没有：跑一次 `--without-ceilings`，读者只能看到家族那一行写着 `1: 1 round(s) from the file queue`，少掉的那一族只剩 `--campaign` 页面上的一句。两处输出于是对"这次没做什么"知道得不一样多。

这一轮补两件事，都落在命令自己的输出与记录里：

1. 命令头新增 `leaves` 行，并把补救命令写在同一句里：

```
families          1: 1 round(s) from the file queue
leaves            1 round(s) that named their file and never recorded their ceilings; measure them with brian evolve --rerun-unrecorded --with-ceilings --rerun-limit 0
```

2. `RoundResult.queue` 不再只写"付了哪个域的那一轮"，还写这次付的是哪一族、这次调用开了什么形状：

```
{"family": "file",    "paid": "SMD", "paid_round": "round-unrecorded.json",
 "shape": {"families": 2, "file_rounds": 1, "ceiling_rounds": 1, "ceilings_skipped": 0},
 "largest_at_start": {...}, "share_after": 0.0}
{"family": "ceiling", "paid": "SMD", "paid_round": "round-named.json",
 "shape": {"families": 2, "file_rounds": 1, "ceiling_rounds": 1, "ceilings_skipped": 0}}
```

只有文件族有 `largest_at_start` 与 `share_after`：只有它推动"未记录份额"。构造这段记录的代码被抽成 `_queue_entry(kind, domain, name, largest_start, progress, shape)`，因为在此之前它只能在跑过 Laya 的整条命令里被观察到——一个只能靠终端才能观察的事实，等于没有测试过。

实测（`var/queue78`，两族各一轮，全部真跑）：`drained 2 round(s) …`, `paid SMD x2`, 两份记录如上；再跑 `--campaign` 时上限欠账行消失。

三条读法：

- **拒绝要带补救**。一句"我少做了一族"而没有下一步命令，等于把推断工作推回给读者；补救命令就在同一句里，成本是零。
- **记录要能回答"这次调用做了什么形状"**。"付了一轮"在两族各一轮和一族两轮下是两种事实（边界 163 的记录侧）。
- **抽出来才可测**。记录的形状一旦只能在端到端跑动里看见，它就会在下一次重构里悄悄消失，而没有任何测试会失败。

**边界 164：一条命令省掉的工作，必须在它自己的输出里带着补救命令。**只说"留下了"而不说"用什么补"，读者仍要做第二次推断，而第二次推断正是本循环反复出错的地方。

**边界 165：记录必须写明产出它的那次调用的形状。**"付了一轮"在"两族各一轮"和"一族两轮"下是两种事实；没有形状的记录只能被读成它最常见的那一种。


### 3.85 队列自己的两本账：付的是哪一族，以及这个事实该落在哪一页

第 78 轮让队列产出的每一轮都写下了 `queue.family` 与 `queue.shape`。写完就没人读了：`--campaign` 数轮数、候选、裁决、拒绝，`--history` 数文件与上限，于是"上限族到底被付过没有"这个问题，只能一份记录一份记录地打开来看。第 79 轮把这两个字段读到页面上。

**战役页多一行**，它数的是"这些轮是谁付的"，不是"付了多少轮"：

```
queue paid        2 round(s) were produced by the re-run queue: 1 ceiling, 1 file
```

同一条线还有第三种状态：第 58–77 轮写下的 `queue` 块里没有 `family`，那些轮确实被队列付过，只是没写是哪一族。把它折进"文件族"会让沉默读成最常见的那个值，所以它单独计数并单独说出来：

```
queue paid        1 round(s) were produced by the re-run queue: 1 ceiling; 1 more say a queue paid them without naming the family (records from before round 78)
```

**历史页在每一轮后面标出谁付的**，并把整个 `queue` 块原样放进 `--json-out`：

```
 3  2026-11-19T17:39:09+00:00  accepted=-  …  [recorded, not applied]  [data/smd/stream-one.json]  [paid by the file queue]
 4  2026-11-19T17:39:09+00:00  accepted=-  …  [recorded, not applied]  [data/smd/stream-one.json]  [paid by the ceiling queue]
```

**这一轮改正了条目本身的前提。** 条目 65 要的是"让 `--unrecorded` 的每一轮标出它是被哪一族付掉的"。`--unrecorded` 列的是**还欠着**的轮——还欠着，就意味着还没有后继记录、没有被谁付过；而"被哪一族付掉"属于**付款者**，属于那份把旧轮退役掉的后继记录，它一诞生就同时带着文件与上限，于是再也不会出现在 `--unrecorded` 里。换句话说，条目要的那一列在该页上永远是空的。能回答这个问题的页面是 `--history`：它列所有轮，包括被付过的那一轮。所以这一轮把条目的后半句改着陆到 `--history` 及其 JSON 上，并在条目里写明为什么，而不是悄悄换一个页面。

三条读法：

- **聚合要认第三种状态**。"没有写"不是某个值的别名；折进去之后，页面会用一个从不存在的分类去回答一个真实的问题（边界 166）。
- **事实属于它描述的那个主体所在的页**。"谁付的"属于付款者，不属于欠账者；把列塞进错误的页面，列会永远空着，而空着的列看起来像"从没发生过"（边界 167）。
- **记录写完就要有人读**。第 78 轮写下 `family`/`shape` 时它就是对的；它只是还没有读者。一个字段的价值不在写入时兑现，而在有人拿它回答问题时兑现。

**边界 166：对记录做聚合时，"字段缺失"必须自成一类，不能折进最常见的那一类。**折进去等于用沉默替用户选了一个答案，而且选得最难被发现：页面会显得完整。

**边界 167：一个事实要放在它描述的主体所在的页面上。**"谁付的"属于已付的那一轮；把它放进列"还欠着的轮"的页面，这一列会永远为空，而空列读起来像从未发生。


### 3.86 合计之外还要按流拆开：一条链接要能从两边读

第 79 轮把队列的账读到页面上，但只读了合计：`queue paid 2 round(s) … 1 ceiling, 1 file`。这个数字回答的是"上限族到底被付过没有"，回答不了"是在哪条流上付的"。而排空可以跨两条流：文件族付 `SMD`、上限族付 `RCAEval RE1-OB`，合计照样是"两族各一轮"，看起来每一本账都被付过一遍。

这一轮把同一个计数按 `queue.paid`（队列当时记在哪个域名下）拆开，紧挨着合计打印：

```
queue paid        2 round(s) were produced by the re-run queue: 1 ceiling, 1 file
paid by domain    RCAEval RE1-OB 1 ceiling; SMD 1 file
```

`queue.paid` 与那一轮的 `stream.source` 不是一回事：一次跨两流的排空里，付款者记的是"这次记在谁的账上"，而不是"这份文件属于谁"。`--json-out` 里同名键 `queue_paid_by_domain`，结构与页面一致。

**第二件事：一条链接要能从两边读。** 被退役的那一份记录写着 `[retired, re-measured as round-…json]`；付款的那一份记录里，`queue.paid_round` 一直写着它替换掉的是谁，但页面没有打印，于是从后继那一行往回看的人，只能靠时间戳去猜。现在继任者的行尾带上镜像的一句：

```
 3  …  [data/smd/stream-one.json]  [paid by the file queue]  [replacing round-unrecorded.json]
 4  …  [data/smd/stream-one.json]  [paid by the ceiling queue]  [replacing round-named.json]
```

没有 `paid_round` 的记录不打印这一段——一个没写前身的继任者，不能靠猜补上一个。

三条读法：

- **合计不能代替分项**。只要动作的单位是流，计数就必须在合计之外再按流说一遍；否则一个"看起来健康"的合计会把"这条流的上限账从没被碰过"藏起来（边界 168）。
- **链接写在一边，就要印在两边**。退役记录与继任记录是同一条关系的两端；只印一端，等于要求读者用时间戳做二次推断，而二次推断正是这条循环反复出错的地方（边界 169）。
- **拆开要有依据，不是多印一行**。`paid` 这个字段从第 78 轮起就在记录里，它记的是队列记在谁名下；用它拆而不是用文件归属拆，是因为"付了哪本账"才是读者要问的问题。

**边界 168：聚合不能代替分项——只要行动的单位是流，每个计数都必须同时按流报告。**一个跨流合计可以看起来每个族都被付过，而其中一条流的一本账从未被碰过。

**边界 169：一条关系写在记录的一边，就要在两边都能读出来。**把"谁替换了谁"只印在被替换的那一行，读者就得拿时间戳配对；配对是推断，而推断会错。



### 3.87 一条关系有两个作者：链接交叉校验，以及为什么只有三种不一致进闸门

第 66 轮把同一条关系印在了两页上：被重跑的轮写 `[retired, re-measured as round-new.json]`，付款的轮写 `[replacing round-other.json]`。两句话都应该对，但它们由**两个不同的命令**写：退休是一个命令（`--retire` 或重跑队列里的 `retire()`），付账是另一个命令（重跑队列的 `persist()`）。中间没有任何代码比较它们。于是账簿可以同时写下"round-old 被 round-new 取代"和"round-new 取代了 round-other"，两句话不可能同时为真，而第 66 轮的页面会把它们都印成事实。

第 81 轮把两端读出来、放在一起比，只做这一件事。

**四种不一致，三种收费。** `replacement_links(records)` 只读 `history(include_retired=True)`，返回 `{round, names, kind, detail}`：

| kind | 条件 | 是否收费 |
|---|---|---|
| `mismatch` | 后继说它取代了 P，而 P 的 `retired.re_measured_as` 指的不是它 | 收 |
| `unretired` | 后继说它取代了 P，而 P 身上没有任何退休标记 | 收 |
| `dangling` | 任一端指向的轮不在账簿里 | 收 |
| `legacy` | 被退休的轮指向的后继没有 `queue` 块（第 78 轮之前写的，或手写的 `--retire --re-measured-as`） | 不收 |

`legacy` 不收，理由与第 33 轮 `legacy` / `missing` 的分法相同：那一端**没法补写**。第 78 轮之前的后继记录根本没有 `queue.paid_round` 这个字段，回填只能是编造。把它计费，`--strict` 会永远红着，然后没人再跑它（边界 171）。但它是历史，不是事实：`--audit` 单独印一段 `N replacement link(s) have no successor-side record …`，`LINK_DEFECTS` 里不含它。

**页面与闸门。** `--history` 顶部先给一句总数，然后把一个发现涉及的两行都标出来：

```
links             1 replacement link(s) disagree with their other end: round-new.json
 2  …  [retired, re-measured as round-new.json]  [data/smd/stream-one.json]  [broken link: mismatch with round-new.json]
 3  …  [paid by the file queue]  [replacing round-other.json]  [broken link: mismatch with round-other.json]
```

`--json-out` 里多了 `links`，与页面同源。`--audit` 在**两条路径**上都印（"每个判定都还成立" 的早退路径与正常路径）：一个账簿缺陷不应该只在有别的判定要重算时才出现。`--audit --strict` 退出 2：

```
1 replacement link(s) disagree, so the two ends of the relation cannot both be true:
  round-new.json             mismatch  round-other.json: the successor names round-other.json, but round-old.json names it as its successor
A link is written by a command; a broken one means the books contradict each other and re-deciding
under today's rules cannot settle it. `--strict` charges these as a build defect.
1 replacement link(s) disagree: the retired record and the successor name different rounds,
or one end points at a round the ledger does not hold. The two ends cannot both be true, and
no re-deciding settles it, so --strict exits 2. `--audit` names each one.
```

退出 2 而不是 1，是第 33 轮定的规矩：重新判定（`--rejudge`）能改变的进 1，重新判定永远解决不了的进 2。一条链接两边矛盾，重算今天的规则不会让它们一致——它只会再算出一份新的、同样可能矛盾的记录。

**遗留的含糊。** 上面的例子里有三份记录参与：`round-old` 说"我被 `round-new` 取代"，`round-new` 说"我取代了 `round-other`"，`round-other` 说"我被 `round-new` 取代"。发现被归到 `round-new`（后继）与 `round-other`（它声称取代的轮）这一对上，`round-old` 只在 `detail` 里被点名、没有被标记。因为"谁提名了同一个后继"需要从**所有**记录里反向收集，而 `replacement_links` 现在的形状是 `{round, names}`，装不下第三个参与者。这不是错，是形状不够：下一次可以让 `names` 变成列表，把提名者一起标出来。

三个读法：

- **一条关系有两个作者，就必须有一处比较。** 两边都写下来，不等于两边都被核对过；两个命令各写一半、没人对账的"事实"，其实是两条互不相关的声明（边界 170）。
- **补不回来的那一端是历史，不能收费。** 计费的前提是有人能付；对一笔没人能付的账收费，结果是闸门被关掉（边界 171，与第 72 轮同一条）。
- **缺陷要印在两条路径上。** `--audit` 的早退路径最容易漏印——那正是读者最相信"没事"的一句。

**边界 170：一条关系有两个作者、却没有一处比较，它不是一条事实，而是两条声明；既然账簿把两端都存下来了，就必须比较它们。**

**边界 171：一端无法补写的不一致属于历史，不能进闸门——否则闸门永远是红的，然后没人再跑它。**



### 3.88 缺陷有几个人，就要点几个人的名；闸门收费的事实必须出现在页面上

第 81 轮让链接校验能发现"两端互相矛盾"，但它有两个没做完的地方，而且方向相反：**说得太少**，和**什么都不说**。

**一、一个发现只点一个名。** 第 81 轮的 `replacement_links` 按"退休记录"的顺序遍历，遇到不一致就写一条 `{round: 后继, names: 某个被声称的前身}`。可三份记录可以同时参与一条关系：

```
round-old   → 我退休后由 round-new 重测
round-other → 我退休后由 round-new 重测
round-new   → queue.paid_round = round-other
```

只有一句话不可能为真：`round-old` 说它被 `round-new` 取代，`round-new` 却说它取代的是 `round-other`。但旧实现把发现记在 `round-new` 名下、只点名 `round-other`，真正的矛盾方 `round-old` 一个字都没提——页面上它那一行没有任何标记，而**没有标记的行默认读作"这行没问题"**。这不是漏印，是误导。

第 82 轮先按"被声称的后继"分组（`nominators: Dict[str, List[str]]`），再对每个提名者单独判一次：后继只可能取代一个记录，其余提名者各得一条 `mismatch`，`names` 变成**列表**，把三个参与者都装进去：

```
 1  …  [retired, re-measured as round-new.json]  [broken link: mismatch with round-new.json, round-other.json]
 2  …  [retired, re-measured as round-new.json]  [broken link: mismatch with round-new.json, round-old.json]
 3  …  [paid by the file queue]  [replacing round-other.json]  [broken link: mismatch with round-old.json, round-other.json]
```

三行都带标记，`--json-out` 的 `links` 同源，`--audit` 的明细也把它们全列出来：

```
  round-old.json             mismatch  round-new.json, round-old.json, round-other.json: round-old.json names round-new.json as its successor, but round-new.json says it replaced round-other.json
```

顺带修掉一个语义问题：发现现在记在**提出不可能那句话的那一轮**名下（`round-old`），而不是记在后继名下——前者才是"需要重新解释"的那份记录。

**二、闸门收费，页面不提。** 第 81 轮把 `broken_links` 接进了 `--campaign --strict`（退出 2），也接进了 `Campaign` 的字段，却没有把它印到 `--campaign` 页面上；只有 `--audit` 会印。于是读者看到 2 退出、页面上找不到任何一行解释它——和第 56/58 轮"闸门收了一笔页面上不存在的费"是同一件事，只是换了一个族。第 82 轮补上：

```
links             1 replacement link(s) disagree with their other end: --audit names each one, and --strict charges them as a build defect
links             1 more have no successor-side record (written before round 78) and are reported, not charged
```

只有历史那一端时（没有可收费的缺陷），句子反过来：`… have no successor-side record (written before round 78); every link that has both ends agrees`。两条分支都要有话说，因为"没有缺陷"和"缺陷都补不回来"在退出码上都是 0，在读者眼里却完全不同。

两个读法：

- **缺陷的参与者要全点名。** 读者在一行的位置判断这一行；没被点名的参与者会被默认读成清白。所以一条发现的 `names` 必须是集合，不是"顺手拿到的那个"（边界 172）。
- **闸门不许收一笔页面上看不见的费。** 退出码是结论，不是证据：读者要能在同一屏上读到"为什么"，否则 2 只是一次无法质询的指控（边界 173）。

**边界 172：一个缺陷涉及几个记录，就要点几个记录的名。** 只点名一部分，等于替其余部分宣布清白——而页面上一行没有标记，默认读法就是"这行没问题"。

**边界 173：闸门可以因为某件事失败，但同一份输出里必须印出那件事。** 只靠退出码传递的失败，读者只能猜是哪一条规则触发的。


### 3.89 补一次就不再有：`legacy` 端的修复，以及为什么修复必须自己招认

第 81 轮把替换关系分成四类，其中 `legacy` 是唯一"补不回来"的一类：后继记录写于第 78 轮之前，`queue.paid_round` 这个字段还不存在，于是退休记录说"我由 round-new 重测"，而 round-new 一个字不说。第 82 轮把它标成"只印不收费"——诚实的做法，但代价是这笔账永远挂在页面最上面，读者每次打开都要重新读一遍。

第 83 轮给它补了一次名，但只补这一种，且补完必须自己招认。

**一、只补 `legacy`。** `mismatch` 与 `dangling` 不进修复：前者是两份记录的**分歧**，抄任何一边都等于在没有证据的情况下选一个赢家；后者连对方记录都不在账簿里，没有可抄的东西。这两类留给 `--audit --strict` 去收费（边界 173 已经在收）。`legacy` 不同：后继**什么都没说过**，所以没有可矛盾的对象，抄退休记录那一句不选赢家。修复按对生成，而不是从发现的 `names` 里取：

```
legacy_links(records) -> [{"retired": "round-old.json", "successor": "round-new.json"}]
```

这条修复本身带一个陷阱，第一次跑就踩到了：第 82 轮把 `names` 改成了**参与者列表**（一个缺陷可能牵涉三份记录），而修复的第一版写的是 `successor_name = str(item.get("names"))`，于是"文件名"变成了 `"['round-new.json', 'round-old.json']"`，哪个文件都不存在，`--repair-links` 对每一份链接都打印 `skipped … the successor has no round file to write to`，`0 of 1 legacy link(s) would be repaired.` 一行字都没写。**修好之后的第一条结论是：`names` 是给人读的集合，不是给程序取名的字段**——需要某一端时，从记录本身重新算。

**二、补完的端必须写清"我是抄来的"。** 修复把 `queue.paid_round` 写进后继的**轮次文件**（不是 ledger：`history()` 先读轮文件、再让 ledger 覆盖，而 ledger 不带 `queue` 字段，所以写在文件上才看得见），并在同一块里加一个 `backfilled`：

```json
"queue": {
  "paid": "",
  "paid_round": "round-old.json",
  "family": null,
  "backfilled": {"from": "round-old.json", "at": "2026-11-20T00:00:00Z",
                 "why": "the retirement mark was the only end of this replacement that existed",
                 "stream": "SMD"}
}
```

`paid` 故意留空：第 80 轮按 `queue.paid` 汇总"每个域付了哪一族"，而修复**不知道**当时的队列会把这笔账记在哪个域上；猜一个，就是把修复的想象写进那张表。`family` 同理留 `null`。

没有这个标记，下一次 `--audit` 会读到两端一致，报告"every link agrees"——而那份一致是修复自己制造的。**让两份声明一致，和让两份声明互相印证，是两件不同的事**（边界 174）。

```
$ brian evolve --repair-links
would write    round-old.json -> round-new.json  (marked `backfilled`, so the agreement is copied, not a second opinion)
1 of 1 legacy link(s) would be repaired.

nothing was written; add --apply to write these 1 end(s).

$ brian evolve --repair-links --apply
wrote          round-old.json -> round-new.json  (marked `backfilled`, so the agreement is copied, not a second opinion)
1 of 1 legacy link(s) repaired.

a repaired end records `backfilled`, so the two ends agree by copying; --audit counts these
separately and does not treat them as independent confirmation.
```

**三、三个页面要同时改口。** 修完以后 `--history` 不再说"every replacement link agrees"，而是：

```
links             1 replacement link(s) were backfilled by a repair: the agreement is copied, not a second opinion
```

`--audit` 也把补写的端单列，而不是并进"没有缺陷"：

```
1 replacement link(s) were backfilled by a repair, so their two ends agree by copying:
  round-new.json
Counted separately on purpose: a copied end is not a second witness, and the repair is not evidence
that the replacement happened.

ceilings          every recorded round was judged under today's ceilings; 1 round(s) do not say which ceilings they were judged under
```

退出码仍然是 0：补写消除的是"缺一端"，不是"两端矛盾"，如果把它也算成缺陷，那么每修一次，闸门就红一次，修复永远无法让账簿变干净（与第 33 轮 `legacy` / `missing` 的拆分同一条理由）。

**四、跑起来才发现的三个小缺陷。**

1. **草稿把可修的链接印成 `skipped`。** `format_repairs` 用 `written` 判断"这行修了没有"，但 dry run 里 `written` 天生是 `False`，于是一份能修的链接被印成"跳过"，摘要还写 `0 of 1 … would be repaired`——**计划（would write）与实际（wrote）是两个事实**，行里必须各占一个字段。
2. **`--repair-links` 不带 `--apply` 时不打印 "add --apply"。** 同一原因：它数的是 `written`，而计划里没人被写，于是提示数为 0，读者拿到的是一份没法执行的计划。
3. **`--history` 明明有一端没写，却说"every replacement link agrees"。** 第 82 轮给 `--campaign` 加了 `legacy` 分支，`--history` 只加了"缺陷"和"一致"两条——**一个页面在两种状态之间只有两个分支时，第三种状态会被悄悄归到其中一边**，这里归到了"一致"。现在 `--history` 把"缺一端"和"补写的端"各印一行，两条都印不出来时才说"每一端都一致"。

三个都是同一类：**判断句用的字段，和读者以为它在判断的事实，不是同一个**。前两个字段叫 `written`（做过），语义却是 `would_write`（能做）；第三个分支少了一条，缺省就落进"没问题"。

**边界 174：一次让两份声明变一致的修复，必须在被修复的那一端记下"这是抄来的"**——否则"一致"就同时包含了"互相印证"和"自己抄自己"，而后者永远无法被读者区分出来。

**边界 175：能改写历史记录的命令，必须能在动手之前把要写的东西写出来给人看。** 计划与执行分开（`--repair-links` / `--repair-links --apply`），因为一旦写进账簿，"原来是什么样"就只剩读者手里的那句记忆。

### 3.90 承诺只能叠在账簿还认的记录上

第 81–83 轮让一条替换关系的两端可以互相核对，也让缺的那一端能被补上。但没有人问过晋升真正要问的那个问题：**产出这个版本的那一轮，现在还是一份别人能相信的记录吗？** 版本通过 `applied` 指名它的产出轮，所以只要那一轮是断链的参与者，这个版本就继承了那份断链。把承诺叠上去，等于把账簿自己都在争议的数字固定下来，而 `--audit` 也裁决不了（边界 170/173）。

两种情形，两个退出码：

| 情形 | 含义 | 有没有命令能清掉 | `--verify` |
|---|---|---|---|
| `broken link`（`mismatch` / `unretired` / `dangling`） | 两本账互相矛盾 | 没有：重新测量也决定不了哪本对 | 2（构建缺陷） |
| `backfilled end`（第 83 轮的补写） | 这一端是抄来的 | 有：重跑那一轮，后继会自己指名前驱 | 1（欠账） |

实测（`var/demo84/`，两个账本各一个版本）：

```
verifying 1 promotion(s) from smd on SMD
  v020  digest.rolling_window = 200  broken link: round-old.json is one of the records in a mismatch link: round-old.json names round-new.json as its successor, but round-new.json says it replaced round-other.json
        not checked: a promise stacked on this record would entrench numbers the ledger itself disputes
...
1 version(s) were left unchecked: 1 carry a replacement link the ledger contradicts, 0 were recorded on an end a repair copied.
A contradiction is a build defect: no re-measurement settles which of the two books is right, so `--verify` exits 2. `--audit` names the link.
No verification file was written for these versions, so they stay due on every run.
```

```
  v020  digest.rolling_window = 200  backfilled end: round-new.json has no successor-side record of its own: the end was copied from the retirement, not written by the queue
        not checked: a promise stacked on this record would entrench numbers the ledger itself disputes
...
A copied end is a debt, not a defect: re-running the round writes a successor that names its predecessor itself, and `--verify` exits 1 meanwhile.
```

三个设计决定：

1. **守卫放在叠承诺的地方，不放在出轮的地方。** 一轮刚跑完、自己写下了记录，它的自评不会被自己的账本反驳；会出问题的是把一个旧版本重新当成"在生效的承诺"时。所以守卫在 `--verify` 的循环最前面，`--apply` 与自动接受都不必改变。
2. **不写验证记录。** 写一条 `unmeasurable` 会让这个版本在 `CLAIM_EXPIRY_DAYS` 内不再被问，恰好是最不希望的结果：它必须在每次跑时都再拦一次，直到账簿自洽。页面因此说"No verification file was written for these versions, so they stay due on every run."
3. **只有矛盾才是构建缺陷。** 抄来的那一端能靠重跑清掉（边界 154/171：清不掉的闸门没人跑），所以它记 1 而不是 2；而矛盾没有任何命令能裁决，记 2，与第 33 轮 `missing` / `legacy` 的分法一致。

两个在实现与实测中发现的问题：

- `Campaign.to_dict()` 从来没有序列化过 `broken_links`：第 82 轮设了字段、印在了页面上，脚本却看不到它。本轮补了 `backfilled_links`，也把这个事实记下来 —— **打印过的字段不等于导出过的字段**。
- 补写出来的那一端被当成了一次队列付款。`var/demo84/copied` 上 `--campaign` 打出 `queue paid 0 round(s) were produced by the re-run queue but none of them names its family; 1 more say a queue paid them without naming the family`，而那个账本里唯一的 `queue` 块正是第 83 轮的补写写的（`paid` 为空、`family` 为空，因为补写不知道哪一族被收过费）。第 79 轮的计数器把"`queue` 存在但 family 为空"一律读成"有队列付过费但没说族"，于是修补动作给自己造了一笔不存在的付款。现在 `campaign()` 跳过带 `backfilled` 的 `queue` 块 —— 已经计入 `backfilled` 列，重复计入付款列会让两列互相矛盾。

边界 **176**：承诺只能叠在账簿仍然一致的记录上 —— 一个版本继承它的产出轮，产出轮若是断链参与者，版本就继承了那份矛盾，此时写下的任何"持有/失效"都在替一份自己都不认的账背书。

边界 **177**：拒绝必须能被一次重测清掉 —— 只有矛盾是构建缺陷，抄来的那一端是欠账；把两者都记成 2，等于让修补过的账本永远红着，而没人会跑一个永远红的闸门。


### 3.91 闸门只管叠承诺的那一刻，已经在跑的版本没人再问

第 84 轮做了一件对的事：一个版本的产出记录如果两头对不上，或者它的端是修复补写的，就不许在它上面叠承诺。但那是一个**叠承诺那一刻**的拒绝。版本可以先被应用，账簿后来才动：

- 某轮被retire、被重测，写下一个和后继对不上的关系；
- 某个人用 `--repair-links` 给一个老记录补上缺失的端。

两种情况下那个版本都还在跑，而没有任何命令会再问一次。`--audit` 只说"1 replacement link(s) disagree"，不说是哪个**在生效的版本**踩在上面；`--campaign` 只数链接和补写；`--verify` 根本没被调用。第 84 轮的 `provenance_block` 已经会回答"这个版本的产出记录还可信吗"，问题是没人问。

**第 71 项的前提本身是错的。** 它写着"让 `--promote` 也走 `provenance_block`"，但 `brian evolve --help` 里没有 `--promote` 这个入口（只有 `--verify`、`--apply`、`--repair-links`、`--history`、`--rejudge`、`--audit`、`--retire`、`--campaign`、`--grade`）。真正漏掉的那一半不是"另一个入口绕过闸门"，而是**没人对已经在生效的东西发问**。所以这一轮把问题掉了个方向：不问"还能不能叠"，而问"已经在跑的是什么"。

#### 部署状态是唯一知道这件事的地方

`var/evolve/domains/<slug>/current.json` 里的 `sources` 把环境变量映射到拥有它的版本号——这是"一个进程启动时会拿什么"的唯一记录。三个新函数都从它出发：

```
_deployment_states(root)      -> [(slug, path)]    # 迁移前的全局 current.json 记作 ''（默认域）
in_force_promotions(root)     -> [(slug, version)] # 去重：两个变量属于同一版本是一个版本，不是两个
in_force_disputes(root)       -> [{domain, version, broken, kind, detail}]
```

`in_force_disputes` 对每个在生效的版本调一次第 84 轮的 `provenance_block`，把结果原样搬出来。`kind` 是给人看的标签（`broken link` / `backfilled end`），`broken` 是判断本身——因为这个判断只有同时认识两个标签的 `promote.py` 做得了。

#### 页面、战役、闸门，三处都读到同一个事实

`--audit`（以及 `--campaign` 里那张表）多出这一段，并且只在这里才说得出"在跑"：

```
1 version(s) in force sit on a record whose provenance the books dispute:
  smd              v020  broken link   round-old.json is one of the records in a mismatch link: round-old.json
                                       names round-new.json as its successor, but round-new.json says it replaced
                                       round-other.json
A version is deployed, so refusing to stack it is no longer available. A contradiction is
still a build defect (`--strict` exits 2); a copied end is a debt, named here, not charged.
```

`--campaign` 的 `links` 段多一行 `links  1 version(s) in force sit on a record the books dispute: v020`，`--json-out` 里是 `in_force_disputed`（每条带 `domain`、`version`、`broken`、`kind`、`detail`）。真实账簿上是 `in force: [('', 19), ('rcaeval-re1-ob', 19), ('smd', 20)]`、`disputes: 0`：三个在生效的版本，没有一个踩在争议记录上。

#### 两种争议，两个结论

| 争议种类 | 含义 | `--strict` | 能不能清掉 |
| --- | --- | --- | --- |
| 断链（`broken link`） | 两头说的是不同的替换关系 | **exit 2** | 不能：没有哪次重测能决定哪一本账是对的 |
| 补写端（`backfilled end`） | 端是修复从 retire 记录抄来的 | 报告，**不收费**（exit 0） | 不能：要重写那个端只能重跑那一轮，而那一轮已经在生效 |

补写端不收费的理由和第 84 轮拒绝不了的原因一样，只是位置换了：版本已经在跑，副本并没有让它跑得更差，而"能修好它的命令"要求重跑一个已经在生效的轮次。一条永远清不掉的闸门，最后就是没人跑的闸门（边界 171）。断链仍然收费，因为它是一个**构建缺陷**：账簿互相矛盾这件事本身不会因为时间过去而变好。

#### 两个由运行暴露的缺陷

**一、把标签当成了种类。** 第一版 `in_force_disputes` 存 `"kind": block[0]`，而 `block[0]` 是 `"broken link"` / `"backfilled end"`；`Audit.in_force_broken` 却拿它去比 `LINK_DEFECTS = ("mismatch", "unretired", "dangling")`。一个值和它的取值域不相交，比较永远是假：页面上明明打印着 `broken link`，收费计数却是 0。修法不是让 `evolve` 也认识那两个标签（那会让 `evolve` 依赖 `promote`，而 `promote` 依赖 `evolve`），而是让行里带上判断本身：`"broken": block[0] == LINK_BROKEN`。**一个字段的含义，和读者以为它在比较的事实，必须一致**——这一轮的第 84、83 轮都栽在同一类地方。

**二、同一个缺陷收了两次费。** 第一版在 `broken_links` 的 `return 2` 之后又写了一个 `if in_force: return 2`。这个分支永远不会单独触发：在生效版本踩的断链本来就是账簿里的一条断链，`broken_links` 已经数过它。第二个 `return 2` 不改变任何退出码，只会让读者以为这里有一道独立检查。删掉它，保留一句说明——"so a deployed configuration inherits the contradiction"——因为这个事实**值得说**，只是不值得再收一次费。这正是第 22 轮删掉"构造上不可达的分支"的同一个教训，换了个形状。

#### 这一轮的边界

**边界 178：叠承诺那一刻的拒绝，管不到已经部署的东西。** 一个版本的产出记录可以在它被应用之后才变得有争议（retire、重测、修复都可能后到）。所以"这个记录还可信吗"这个问题必须也对部署状态问一次，不能只在 `--verify` 里问。

**边界 179：已经在生效的补写端只报告，不收费。** 没有任何命令能在版本继续生效的前提下重写那个端，收它的费就是立一条谁都清不掉的规矩；而"抄来的端"也不需要清掉才能让部署继续正确——它本来就在跑。

#### 这一轮之后

`--audit --strict` 现在对"在跑一个踩在断链上的版本"退出 2，对"在跑一个踩在补写端上的版本"只报告。第 72 项留给下一轮：把 `in_force_disputes` 的时间也记下来——同一个版本连续几轮都踩在争议记录上，应该像 `--verify` 的到期一样，从"报告"变成"该处理了"；否则它会安静地留在页面上，而页面上的警告最容易被读成"没关系"。

### 3.92 重算出来的事实没有历史：把"第一次看见"写下来，并且只让能清掉的那一种升级

第 3.91 轮把来源之问问到了已经在生效的版本上：`in_force_disputes` 从每个部署状态的 `sources` 读出在跑哪些版本，逐个调第 3.90 轮的 `provenance_block`。这个答案是**每次运行重算**的，所以它天生只能回答"有没有"，回答不了"多久"——而"多久"恰恰是唯一能让一个事实从"今天这样"变成"该处理了"的东西。

年龄不能从账本里推出来。账本记的是每一轮**变了什么**（记录改写、退休、补写），不是循环**什么时候第一次看见**某个版本踩在一条被争执的链接上。前者是数据的变化，后者是对数据的观察；观察没有别的地方可以记，于是它被写在账本**旁边**而不是账本**里面**：`var/evolve/in_force.json`。

存储的形状是 `{"format": "brian.inforce/1", "observed": [...]}`，键是 `(domain, version, kind)`。三个字段承担三件事：`first_seen` 一旦写下就不再改写（这是年龄的分子），`observations` 每轮加一（说明它不是一次性的误读），`age_days` 每次由当前时刻减出来（所以它永远和这次运行的时钟一致）。本轮已经不再争议的行会作为 `cleared` 回传一次、然后从存储里删除——页面上必须能说"这笔账结了"，而不是继续宣布一个已经不再成立的事实。一个从没有过争议的部署不写这个文件：一个空存储会暗示"曾经有过"。

升级只给一种行：**补写端**（`backfilled end`，第 3.91 轮的修复写出来的那一端）。断链（`mismatch`/`unretired`/`dangling`）不在升级范围里——它一旦存在就已经按第 3.90 轮收 exit 2，因为没有任何一次重测能决定哪一本账是对的，加一条年龄只是让同一个收费多等几天。补写端不同：它恰好有一个补救动作，就是把在跑的版本再量一次（`--verify`），所以过期的补写端收 **exit 1**，并且这次的措辞必须点名那条命令：

```
1 version(s) in force have sat on a copied record for longer than the 14-day grace period
with no re-measurement newer than the objection, so --strict exits 1:
`brian evolve --verify --verify-version <n> --stream-file <the file that version measured>`
clears it, because the copy is about the record, not about the number.
```

同一个版本在 20 天前被看见、之后一次都没再量过，审查页上是这样的：

```
  smd              v020  backfilled end round-new.json has no successor-side record of its own:
                         ... [first seen 20.0 day(s) ago, seen 4 time(s)]  [stale: no verdict newer than the objection]
1 copy(ies) have stood longer than 14 day(s) with no verdict newer than the
objection, so `--strict` asks for a re-measurement of the deployed version: that is the
one command that clears it.
```

写下一个比首次看见更新的 `verification.checked_at` 之后，段落和行尾标记一起消失，`--audit --strict` 退出 0：测量比异议新，而异议说的是记录，不是数字。战役页同一时间给的是 `v020 (20.0 day(s))` 和一行"站在宽限期之外"，`--json-out` 里是 `in_force_stale` 与 `in_force_cleared`。

这一轮现场暴露了两个缺陷，都属于同一类：**同一个判断被写了两遍**。

第一处：页面按年龄升级，闸门按"年龄 + 没有更新的测量"升级。于是补上一个一天前的验证之后，`--audit --strict` 退出 0，而审查页还在印"`--strict` asks for a re-measurement"——页面在许一个闸门不做的承诺。修法不是让页面少说一句，而是把裁决放进 `stale_in_force_disputes` 一处，页面和闸门读同一个列表（`Audit.in_force_stale`）；页面的行尾标记也从"年龄过线"改成"在这份裁决里"。

第二处：时钟。工装夹具用一固定的 `NOW` 写了"20 天前"的时间戳，而观察走的是真实时钟，于是同一行被算成 8.3 天：年龄只有在同一个时钟下才有意义，而"现在"是唯一一个只有一个来源的量。夹具改成由真实时钟减出来的时间戳。

**边界 180：一个每次运行都从账本重算出来的事实，说得出"是不是"，说不出"多久了"。** 年龄只可能是关于观察的，不是关于数据的，所以它必须被写在账本旁边，而且必须只写一次（首次看见），不能每次覆盖。

**边界 181：年龄不是补救。** 只升级"有命令能清掉"的那一种欠账，并在升级的句子里点名那条命令；已经按缺陷收费的那一种不要再叠一层年龄。


### 3.93 清掉一笔债，要说清是谁清的

第 3.92 轮让"一个每次重算的事实"有了年龄：把首次看见写进 `var/evolve/in_force.json`，过期升一级。这一轮问的是它退场的那一半——一行争议**消失**的时候，究竟是**被解决**了，还是**被忘掉**了。

这两种事件在页面上长得一模一样。第一种是有人把在跑的版本重测了一次，测量比异议新，补写端因此不再是欠账；第二种是账本干脆不再复现这条异议（记录被改好、被退休、或者被谁删掉了），没有任何测量参与。前者是裁决，后者是观察的缺失。第 86 轮的存储只保留**当前**有争议的行，消失的行回传一次然后删掉，所以它只能说"不再争议"，分不出这两者——而"说不清"在账本上会自动倒向便宜的那一边：债务消失了，账单上看起来就像付清了。

于是存储升到 `brian.inforce/2`，在旁边多一份 `cleared` 历史，每行都带**引用**：

```
  smd              v020  backfilled end (first seen 2026-11-19T19:05:39+00:00, stood 0.0 day(s),
                         cleared by no measurement: the ledger no longer reproduces it)
A dispute that left the ledger without a measurement is not resolved, it is unobserved:
counted apart on the campaign page, and charged by nothing (boundary 183).
```

同一个账本，如果消失之前写过一份更新的验证，同一行读作 `cleared by a re-measurement on 2026-11-19T19:18:18+00:00`。判定规则只有一条：把该版本的 `verification.checked_at` 与这一行的 `first_seen` 比较，新的算 `verification`，其余一律 `unexplained`（不是"算作解决"，是"没有证据说它解决了"）。历史有上限（200 条），被裁掉的条数写进 `trimmed` 并由战役页印出来：一份被裁剪却不说的历史，看起来是完整的。

战役页把两种清掉分开报，绝不加总：

```
links             1 dispute(s) left the in-force list: 1 cleared by a re-measurement (median 0.0 day(s)), 0 with no measurement on file
```

没有测量的清除**不进闸门**。理由和第 3.91 轮"在跑的补写端只报告不收费"是同一个：这个存储是本机的观察文件，不是账本的一部分；一台新机器、一次删库、一次目录搬迁都会让每一行看起来"没有测量"。一个因为本地文件缺失就永远红的闸门，最后没人跑（边界 154、171）。

这一轮现场暴露的缺陷属于另一类：**一个页面因为无关事实的缺席而沉默**。`format_audit` 只在 `result.links or result.backfilled` 非空时才调用 `_link_lines`——那是第 83 轮的写法，当时它还只印这两种块；第 85–87 轮把"在跑的行""过期行""清除历史"都挂到了同一个打印机上，但闸门没跟着改。结果：战役页说"有 1 笔争议离开了在跑清单"，而审查页对这件事一个字都不印。修法是把判断收进一个属性 `Audit.link_content`，两处调用点读同一个判断——这已经是"闸门与内容各自演化"的第三个位置（第 3.88 轮的边界 173、第 3.92 轮的页面/闸门不同裁决），所以这次不是补一个条件，而是让判断只有一个定义。

第三个教训还是时钟，和上一轮同源：测试里 `first_seen` 用的是固定的 `NOW`（2026-12-01），而验证时间戳用的是真实时钟，于是"更新的测量"被判成旧的，引用落在 `unexplained`。**一个比较只在同一个时钟下有意义**；夹具要么全部走固定时钟，要么全部走真实时钟。

**边界 182：只保留当前状态的历史，说不出它是怎么结束的。** 一笔债退场时，"被解决"和"不再被看见"必须分成两条记录，否则账本会自动选择更便宜的那条解释。

**边界 183：没有测量的清除只报告，不收费。** 观察存储是本地的、可丢的，闸门不能因为一个不在账本里的文件缺失就永远红。

**边界 184：一个打印机的闸门，必须点名它全部的主题。** 只列了其中一种事实的条件，会在其余事实单独出现时让页面沉默——而沉默读起来像"没有问题"。


### 3.94 诊断指名的命令必须真的能跑

第 86 轮把"躺了太久的抄写端"升级成一条收费：`--strict` 退出 1，并且打印出点名的那条命令——`brian evolve --verify --verify-version 20 --stream-file …`，意思很明确："去把这个版本重新量一次。"第 84 轮给 `--verify` 加的护栏恰好拒绝的就是这种记录：`backfilled end` 上的承诺不许再叠。两轮各自都对，合起来就成了一句不可能执行的话：**红灯亮着，而它指的那只开关是被自己焊死的。**

这一轮本来只做 §6 第 74 项（让 `--verify` 把清账自己写进存储，省掉事后从时间戳反推），一接就撞上这件事。修法不是撤掉第 84 轮的拒绝，而是把两件事分开：

- **拒绝叠承诺**（默认，仍然退出 1）：`not checked: a promise stacked here would inherit an end a repair copied; --accept-copied measures it without applying`
- **愿意测量但不落承诺**（`--accept-copied`）：`measured, not applied: the record's end was copied, so the deployment is left alone`

测量和落承诺是两件不同的事。拒绝后者的理由——"这份记录自己都证不明自己的来源"——不构成拒绝前者的理由：看清楚它现在跑到什么程度，恰恰是唯一能让这笔债有下文的事。量完以后 `payload["verification"]` 照常写、`record_clearing` 把这次观察答掉，但 `apply_promotion` 一步都不走。

```
smd              v020  backfilled end round-new.json has no successor-side record of its own: the end
                       was copied from the retirement, not written by the queue
                       [first seen 20.0 day(s) ago, seen 4 time(s)]  [stale: no verdict newer than the objection]
… --verify --verify-version 20 --stream-file data/smd/stream-one.json --accept-copied
measured, not applied: the record's end was copied, so the deployment is left alone
v020  unmeasurable  the claim or the re-measurement has no recall at the budget
cleared         1 in-force observation(s) were answered by this check:
smd              v020  backfilled end stood 20.0 day(s)
A measurement answers the objection, not the copy: the deployment state is left alone …
```

答掉之后再跑 `--audit --strict`，退出码是 **0**——不是因为这个版本通过了检查（它这次是 `unmeasurable`），而是因为"有没有人量过"这个问题有了答案。闸门收的是"没人回答"，不是"答案不好看"，这是边界 177 那条"拒绝必须是能被一次重测清掉的"的同一句话。

第二半是**谁写的这条引用**。第 87 轮的清账是事后从 `first_seen` 和 `verification.checked_at` 比出来的，于是同一个事实是否可见，取决于账簿还留不留着两端：改一下记录、把轮退役、或者在没看见那次异议的检出里跑审计，引用就消失了。`--verify` 在知道的这一刻就能写下来，所以 `record_clearing` 写 `cited_by: measurement`，事后反推的写 `cited_by: audit`，页面把两者念出来：

```
Cleared
1 version(s) in force are no longer disputed, and the ledger forgets them now:
  smd  v020  backfilled end (first seen …, stood 20.0 day(s), cleared by a re-measurement on … (written by that measurement))
links             1 of those clearings were written by the measurement itself; the rest were deduced later from the ledger
links             clearings by domain: rcaeval-re1-ob 0 by measurement, 1 without; smd 1 by measurement, 0 without
```

三件实现在写的时候才暴露的事：

1. `record_clearing` 必须**留下 `broken` 行**。测量能回答"这份记录现在跑到什么程度"，回答不了"两份记录谁对"，所以一张矛盾行不会因为量了一次就消失。
2. 分域那一行只在真的有第二个域时印。一个域的账本该说"全部在这一条流上"，而不是打一行只含一个域的"按流拆分"。
3. 两个测试缺陷是同一类：一处把 `audit()` 当成会读存储的函数（它只渲染传进去的 `in_force_cleared`，读存储的是 `cli`），一处用了 `legacy` 的夹具去断言 `broken`——夹具不产出它名字所说的那种缺陷，断言落在空列表上。

**边界 185：诊断指名的补救，必须是那条命令真能做到的事。** 一条收费若点名 `--verify`，而 `--verify` 对这类记录只会拒绝，那么这条费用永远付不掉，闸门就变成一盏常亮的红灯。

**边界 186：测量和落承诺是两件事，拒绝后者不能顺带拒绝前者。** 抄写端上不许叠承诺，是对的；但它恰恰是唯一需要被量一次的记录——不量，这笔债连"有人回答过"都没有。

**边界 187：当场就能写下的事实要当场写，并标明是自己写的。** 事后从时间戳反推的引用依赖账簿两端都还在；写下 `cited_by` 之后，读的人能分清"量出来的"和"推出来的"。


### 3.95 比不了，不等于答过了

第 88 轮让 `--verify` 在跑完之后写清账记录，于是"红灯为什么灭"有了出处。但那次实现只看了一件事：
`record_clearing` 被调用过。于是 `unmeasurable` 也清账——而这正是本轮实测里发生的事：3 个窗口的流
产不出 at-budget 召回，复核跑完了、判决是"判不出来"，那一行在役异议**消失**了，`--strict` 从 1 变 0。
更糟的是它之后的表现：行被删掉的位置上，下一次 `--audit` 会重新观察出同一条异议，`first_seen` 是**今天**。
也就是说，一次比不了的复核不仅答掉了异议，还把它变成了一个刚出现的新债——14 天宽限期重新起算，
过期收费再也不会触发。

> 一个非回答能重置的时钟，不是时钟。

所以本轮只做一件事：把"跑过"和"答了"分开。`record_clearing(..., tested=)` 记录这份清账有没有真的
检验承诺（`tested: false` 当 `verdict == unmeasurable`）；`unmeasurable` 的行**留在** `observed` 里，
`first_seen` 不动，只挂一个 `untested: {at, verdict, reason}` 说明"谁在什么时候试过、为什么没比成"；
`stale_in_force_disputes` 只承认 `verdict != unmeasurable` 的复核，于是红灯继续红。

| 复核判决 | 行 | `first_seen` | 清账记录 | `--strict` |
| --- | --- | --- | --- | --- |
| `holds` / `failed` | 移除 | 不再需要 | `tested: true`，`cited_by: measurement` | 0（债被答掉） |
| `unmeasurable` | 保留 | 不动，`untested` 记本次尝试 | `tested: false`，单独计数 | 1（红灯继续） |

两个直播，同一份 20 天前的在役副本，只差复核能不能比：

```
$ brian evolve --audit --strict                       # demo89/unanswerable，3 个窗口的流
  smd  v020  backfilled end …  [first seen 20.0 day(s) ago, seen 4 time(s)]  [stale: no verdict that could test the claim]
1 copy(ies) have stood longer than 14 day(s) with no verdict that could test the
claim about it, so `--strict` asks for a re-measurement of the deployed version: that is
one command that clears it.
                                                    # 退出 1

$ brian evolve --verify --verify-version 20 --stream-file data/smd/stream-one.json --accept-copied
        measured, not applied: the record's end was copied, so the deployment is left alone
  v020  digest.rolling_window = 200  unmeasurable: the claim or the re-measurement has no recall at the budget …
not answered  1 in-force observation(s) were measured, but the check could not test the claim:
  smd              v020  backfilled end  the claim or the re-measurement has no recall at the budget …
The row keeps its first sighting: a comparison that compared nothing is not an answer,
and it must not reset the clock it was supposed to close (boundary 188).

$ brian evolve --audit --strict                       # 复核之后
  smd  v020  backfilled end …  [first seen 20.0 day(s) ago, seen 6 time(s)]  [stale: a check on 2026-11-19T19:46:04+00:00 could not test the claim]
1 of them were measured and the check could not test the claim (the claim or the re-measurement has no
recall at the budget, so the promise cannot be checked either way):
measuring again on the same stream compares nothing again, so the remedy is the file that version recorded,
or retiring the version whose claim cannot be checked (boundary 188).
                                                    # 仍然退出 1（第 88 轮的同一序列会退出 0）
```

同一份副本，把承诺的几何指到它真正被测量的那条流上（828 个有标签窗口，与 `v020.stream` 一致），
同一个命令就有了答案：

```
$ brian evolve --verify --verify-version 20 --stream-file data/smd/stream-real.json --accept-copied
        measured, not applied: the record's end was copied, so the deployment is left alone
  v020  digest.rolling_window = 200  FAILED and reverted: promised recall 0.507 at the 10% budget, re-measured 0.060 (below 0.456)
cleared         1 in-force observation(s) were answered by this check:
  smd              v020  backfilled end stood 20.0 day(s)
                                                    # 退出 1（复核失败，版本被撤回），但这一次是"答了"
$ brian evolve --audit --strict                       # 退出 0：债被真正的测量答掉
```

三条读法。

1. **清账与复核是两个动作，答案却有三种结局。** `holds` 和 `failed` 都是回答（配置被留下或被撤回，
   问题都解决了）；`unmeasurable` 不是回答，它只是一次尝试。把三者折成"跑过就算答过"，等于让
   `--verify` 的第三种结局变成万能钥匙——而那恰好是第 26 轮引入 `unmeasurable` 时要避免的事。
2. **两个页面说同一件事，靠的是同一份判断。** 行上的 `[stale: …]`、页尾的整段、闸门的那一句，都读
   `in_force_stale` / `in_force_untested` 两个由 `promote.py` 算出的列表；第 86 轮已经吃过一次"页面
   升级、闸门没升级"的亏，本轮把这个模式用在第二处。
3. **措辞也是判断的一部分。** 原来的句子说"没有比异议更新的复核"，本轮之后它不再成立：复核跑了，
   只是比不了。句子改成"没有能检验这条主张的复核"，同时改名的那条断言
   （`tests/test_in_force_age.py`）跟着改——一条没人改的断言会把旧措辞钉在代码里（边界 190）。

两个实现缺陷。其一，`var/patch_89.py` 把 `src/brian/cli.py` 从它提交时的 CRLF 写成了 LF，33 行的改动
在 `diff --stat` 里变成 8470 行；让每个补丁脚本先读文件自己的换行约定（`var/fix_89.py` 会打印
`(LF)` 还是 `(CRLF)`），否则一次补丁换来整个文件的 diff。其二，页面把这次失败尝试的原因写成
`measured again by --verify`——那是 `cli` 递给 `record_clearing` 的记账短语，不是"为什么没比成"，于是
页面用错误的原因点名了错误的补救；改法是把尝试自己的原因一起递进去（`attempt_reason`），页面才有资
格说"该换文件、还是该退版本"。


### 3.96 一次清账必须说出是凭哪份测量清的

第 87 轮让"债还清了"这句话带上它是被谁清的（复核 / 只是账本不再复现）。那句话里有一个时间戳，但
没有证据：`record_clearing` 没有流文件这个字段，存储里的每条 `cleared` 都是一行匿名的"已清"。于是
`--campaign` 能说"这笔债被复核答掉了"，读者却打不开那份让人们相信它的测量——这正是第 87 轮修过的
同一个缺陷，往下落一层：**判决本身也是一个主张，它必须说出自己凭的是什么。**

```
$ brian evolve --verify --verify-version 20 --stream-file data/smd/stream-real.json --accept-copied
  v020  digest.rolling_window = 200  FAILED and reverted: promised recall 0.507 at the 10% budget, re-measured 0.060 (below 0.456)
cleared         1 in-force observation(s) were answered by this check:
  smd              v020  backfilled end stood 20.0 day(s), answered on data/smd/stream-real.json
```

`answered on …` 是"结账"的那一页；链本身在 `--history` 上，`stream_path` 与第几次清账并排列出：

```
$ brian evolve --history
clearings        1 recorded clearing(s); each names the measurement that earned it:
  v020  attempt 1  2026-11-19T20:23:08+00:00  failed        data/smd/stream-real.json
```

`--campaign` 按证据所在的那份流文件分开计数，理由和第 80 轮把付款按流拆开一样——合计说不出是哪一份
停了：

```
$ brian evolve --campaign
links             1 dispute(s) left the in-force list: 1 cleared by a re-measurement (median 20.0 day(s)), 0 with no measurement on file
links             1 of those clearings were written by the measurement itself; the rest were deduced later from the ledger
links             clearings by stream file: data/smd/stream-real.json 1
```

旧条目（字段出现之前写的）没有文件，页面必须把它印成"没有名字"，而不是留空——一个空字段读起来是
"当时不需要测量"：

```
$ brian evolve --audit
1 version(s) in force are no longer disputed, and the ledger forgets them now:
  smd   v020  backfilled end (first seen 2026-10-30T20:23:55…, stood 20.0 day(s), cleared by a
              re-measurement on 2026-11-19T19:23:55… of (unnamed stream))

$ brian evolve --campaign
links             clearings by stream file: (unnamed) 1
```

三条读法。

1. **同一件事在三个页面上有三个主语。** `--verify` 是"这一次结账"，它说这次凭哪份测量；`--history` 是
   这条版本的历史，它说第几次、什么时候、什么判决、哪份文件；`--campaign` 是全账汇总，它按文件分开
   数。第 85 轮的教训在这里翻了个面：那时是"页面升级了、闸门没升级"，这次是"存储升级了、**结账那一页
   没升级**"——`record_clearing` 收到了 `stream_path`，而 `--verify` 的收尾行仍旧只说 `stood N day(s)`，
   证据只活在存储里（边界 191）。
2. **ordinal 是链条的一部分。** 没有 `attempt`，"清过两次"和"清过、又被重新异议、再清一次"是同一句
   话；而这两种情况对读者的意义完全相反：前者说明同一个测量被重复接受，后者说明异议还在反复出现
   （边界 192）。
3. **空白必须被印成空白。** `(unnamed stream)` 不是占位符，它是第 87 轮之前的历史事实：那些条目确实
   没有记录文件。把它省略，页面就在替旧记录说谎（边界 193）。

一个实现上的发现：`--audit` 的清账块印的是**这一次观察**（行在这一跑里消失），已写下的清账在任何一次
后续 `--audit` 里都不会再出现——所以"这条清账凭的是哪份测量"如果只挂在 `--audit` 上，它只会出现在
恰好经过的那一刻。这也是为什么上面那三页要各说各的主语，以及为什么本轮最后又给 `--verify` 的收尾行
补了文件名：链条不能只存在于存储里。

**边界 191：清账的判决本身是一个主张，必须点名它凭的证据（文件与时间）。** 一个只说时间、不说文件
的"已清"，读者无法核验，也无法区分"复核了这份流"和"复核了另一份流"。

**边界 192：ordinal 是链条的一部分。** 没有序号，"清过两次"与"清过、被重新异议、再清一次"是同一行
文字。

**边界 193：没有名字的测量必须被印成"没有名字"。** 空字段会被读成"当时不需要测量"，而那是页面替旧
记录编出来的事实。


### 3.97 凭据本身也会过期：结账时看一次，读的时候再看一次

第 90 轮让每一笔结账都写出证据（文件 + 时间 + 第几次）。这一轮问一个更基本的问题：**那份文件还在吗？**

两个方向都得查，而且必须查在不同的时刻。

**写的时候查**：`record_clearing` 先算 `evidence = _evidence_state(root, stream_path)`（`present` / `missing` /
`unnamed`），把它存进条目，并把"是否真的答过"改成 `tested = tested and evidence != missing`。文件名打错、
流被移走、绝对路径来自另一台机器——这些情况下这条记录不是答案，是尝试：观察行留在 `in_force.json` 里，
`untested` 里记下 `evidence`，下一次审计照旧催。这和第 89 轮"比不了不等于答过了"是同一条规矩的第二次应用：
**一次失败的开脱不能把时钟重置。**

**读的时候再查**：`clearing_evidence(entries, root)` 只看当前 checkout，返回
`{present, missing, unnamed, missing_files}`，**不写回存储**。理由写在函数的 docstring 里：这是"读者手上这份
checkout"的事实，不是"结账那一刻"的事实；把它存下来，记录就会随着文件系统变化悄悄变旧（边界 194）。存进去的
`evidence` 是历史，算出来的 `missing` 是现在，两个都要出现，谁也不许覆盖谁。

三个页面各说自己的那一段：

- `--verify` 说这一次结账（第 90 轮的 `answered on <file>`），文件不在就压根不会有这一行；
- `--history` 说链条：`clearings` 块列出每笔的版本、`attempt`、时间、判定与文件，条目文件不在就追加
  `[evidence missing]`，另起一行 `evidence  N of them name a file this checkout cannot open: …`；
- `--campaign` 说总计：`clearings by stream file: <file> N (last <date>)`，加一行
  `links  N clearing(s) name a file this checkout cannot open: <files>`。

现场证据（`var/demo90/answerable`，先结账一笔，再把它凭据的文件改名）：

```
clearings        1 recorded clearing(s); each names the measurement that earned it:
evidence         1 of them name a file this checkout cannot open: data/smd/stream-real.json
  v020  attempt 1  2026-11-19T20:36:48+00:00  failed        data/smd/stream-real.json  [evidence missing]
```

```
links             clearings by stream file: data/smd/stream-real.json 1 (last 2026-11-19T20:36:48+00:00)
links             1 clearing(s) name a file this checkout cannot open: data/smd/stream-real.json
```

文件还在时，同一页只打印 `(last …)`，没有第二行——"缺"和"暂时都在"必须是两句话，这是第 76 轮"跳过的族要说
出来"的同一课，只是这次的对象变成了凭据。

**现场发现的第三个缺陷**：`--verify --stream-file data/smd/stream-real.json` 在文件被移走后不是报错，而是一个
`FileNotFoundError` 的 traceback（`load_stream_file` 从 `_evolve_streams` 里抛出来）。写时检查永远走不到，
因为它要先测完才写；而且异常退出码是 1，和"检查没通过"撞车。修法是测量之前先 `Path(args.stream_file).is_file()`，
不在就报错、把版本记录里那份文件作为建议一并打印，退出 2：

```
cannot read data/smd/stream-real.json: no such file
the promise records the file it was measured on: data/smd/stream-one.json
a measurement on a file this checkout cannot open answers nothing, so nothing was cleared:
pass --stream-file naming a file that still exists, or restore the one above.
```

**边界 194：关于"今天这份 checkout"的事实只能算，不能存。** 存进去的那一刻它是真的，之后每删一个文件它就
悄悄变成假的，而记录不会自己更新；存下来的应当是"当时我看见它在那儿"，读的时候再算一次"现在还看得见吗"，
两句话分开写。

**边界 195：凭据打不开的结账只是尝试，不是答案。** 承认一笔债需要能被打开的证据；打不开就等于没测，观察行
必须留下、时钟不能重置。

**边界 196：命令的输入也是判断的一部分。** 一个不存在的输入必须在测量之前被报告，而不是以异常结束——异常既
没有说出补救办法，又把"输入错了"和"检查没通过"混成同一个退出码。

### 3.98 换一份凭据也要说出来：指名文件不见了怎么继续，以及坏链接有多旧

第 91 轮让每一次清账都指名凭据，并且在写入时和读的时候各检查一次文件在不在。但检查结果只有两种：在，就照常记；不在，就报错退出。一条已经在生效的债务因此可能永远清不掉——凭据被人挪走、仓库换了检出、文件名改过，都不是"账算错了"，可是 `--verify` 连测都不肯测，`--strict` 于是永远红着（边界 185 的另一半：命令要能跑，还要跑得下去）。

第 92 轮把"指名文件不在"拆成两条路：轮流**换一份凭据**，或者**说清楚为什么换不了**。换的判断只有一句：**每一个要检查的版本都指向同一份被记录的文件，并且那份文件在这个检出里存在**。

```
stream file    data/smd/stream-real.json  (substituted: data/smd/missing.json does not exist in this checkout)
verifying 1 promotion(s) from smd on SMD
  v020  digest.rolling_window = 200  backfilled end: round-new.json has no successor-side record of its own: …
        measured, not applied: the record's end was copied, so the deployment is left alone
  v020  digest.rolling_window = 200  FAILED and reverted: promised recall 0.507 at the 10% budget,
        re-measured 0.060 (below 0.456)

cleared         1 in-force observation(s) were answered by this check:
  smd              v020  backfilled end stood 20.0 day(s), answered on data/smd/stream-real.json
```

替换这件事必须写在**它发生的地方**，也必须写进记录：`record_clearing(..., substituted_from=…)` 把这个来源存进条目，`--history` 于是同时印两件事：

```
clearings        2 recorded clearing(s); each names the measurement that earned it:
  v020  attempt 1  2026-11-19T20:49:54+00:00  unmeasurable  data/smd/stream-one.json  [substituted for data/smd/missing.json]
  v020  attempt 2  2026-11-19T20:55:13+00:00  failed        data/smd/stream-real.json  [evidence missing]  [substituted for data/smd/missing.json]
```

第二次之所以多一个 `[evidence missing]`，是因为演完"替换"之后把那份额外文件真的删掉了——记录里写的是"当时那份在"，读的时候再看一次"现在这份在不在"，两件事分开放（边界 194 的延续：今天的事实是算出来的，不是存下来的）。

换不了的时候，理由要具体到能指出下一步。四条句子对应四种情形：

- `the recorded file cannot stand in: 1 version(s) to check do not say which file they were measured on: v020`
- `the versions record different files, and picking one would decide which promise gets graded: …`
- `the file the promise was measured on is also missing: data/smd/stream-real.json`
- `no version records which file it was measured on, so there is nothing to stand in`

第一版只写了 `if usable:` 的那一支，"没有任何版本记录文件"时**一句话都不印**，测试一跑就露出来——回绝必须有一个能读的理由，而不是静默退出（边界 196 的同一件事：输入是判断的一部分）。

坏链接还有第二个问题：它只被数了一次，没人说它有多旧。数量说明"账不完整"，日期才说明"从哪天起不完整"，而两者之间隔着好几个星期的时候，读者无从知道该先开哪一个。所以 `clearing_evidence(..., now=…)` 多带一个 `oldest`（最小 `checked_at`，含版本、文件、年龄），`--campaign` 在坏链接那一行下面把它印出来：

```
links             1 clearing(s) name a file this checkout cannot open: data/smd/stream-real.json
links             the oldest of them was written 2026-11-19T20:55:13+00:00 (v020, 0.0 day(s) ago)
```

这条最旧的链接是"该先开哪一份"的答案，也是唯一一处没有别人能替你看的地方。

本轮的两个过程缺陷也记在这里，因为它们是同一类错误的两面：

- 第 91 轮把 `tests/test_clearing_evidence_file.py` 以 **CRLF** 提交了（本仓库只有 `src/brian/cli.py` 是 CRLF），原因是 `var/fix_endings.py` 用的是手写 `FILES` 清单，**新文件从来不在清单里**。改成遍历 `git ls-files`、按路径套约定之后，这类错误就与"有没有记得加进去"无关了。
- 一次只替换 `def` 行的 `edit` 把两个测试并成了一个并截断了文件尾；恢复方式是 `git checkout --` 之后**整份脚本重建**。要重构的文件不要一块块打补丁。

**边界 197：替换必须写在发生的地方。** 一次判定是关于某份文件的；输入被悄悄换掉，一个人会把一条流上的测量读成另一条流上的答案。

**边界 198：兜底必须唯一。** 记录里有好几份文件时，挑一份就等于替读者决定"给哪个承诺打分"，所以回绝比挑更诚实。

**边界 199：坏链接的年龄把"账不完整"变成"从哪天起不完整"。** 最旧的那一条就是读者该先打开的那一份，而且它没有任何别人能替他看。

### 3.99 修一次检查，不等于修好了记录：`dead_record_paths` 与 `--set-stream-path`

第 92 轮让 `--verify` 在指名文件不存在时改跑记录里的那份，并把清账凭据的年龄印出来。两件事都做对了，却都只让
读者**看见**一件事：某个承诺指着一份在这个检出里打不开的文件。看不见的那一半是：这份记录没有任何命令能改。
于是每次检查要么替换、要么拒绝，记录永远不会学到。

这一轮先把对象搞清楚了。第一版按"同一个版本连续替换两次就判记录过期"来做，跑完发现判错了东西：替换换掉的是
**调用者指名**的文件，连续替换是调用者的习惯（脚本里写死的旧路径），`--set-stream-path` 修不了习惯。真正修得了
的是记录本身——`v020` 说自己在 `data/smd/stream-real.json` 上量过，而这份文件已经不在了。

于是：

1. `promote.dead_record_paths(root)` 扫 `var/evolve/versions/*.json`，列出 `stream_path` 非空且在当前检出里
   打不开的版本，并带 `in_force`（部署状态里是否有人在用）。这是**读时计算**，不写进任何存储：答案是"这个检出
   里有没有这份文件"，文件一挪答案就变（边界 194）。
2. `--campaign` 在 links 段输出：

   ```
   links             v020 records data/smd/stream-real.json (in force), which this checkout cannot open: fix the
                     record rather than the check -- brian evolve --set-stream-path v020=<a file that exists>
   ```

3. `brian evolve --set-stream-path v020=data/smd/stream-moved.json` 做修复本身：目标文件必须存在（修完指着空处，
   修好了文书、丢了证据，边界 195），旧值写进该版本的 `stream_path_history`（`from`/`to`/`at`/`why`），
   返回 `changed`，第二次执行什么都不做。命令自己说清楚："Nothing was re-measured by this call."——修记录和在
   新文件上重测是两件事（边界 186 的同一条规则）。

实测（`var/demo90/answerable`，Laya 在线，把 `data/smd/stream-real.json` 改名之后）：

```
links  v020 records data/smd/stream-real.json (in force), which this checkout cannot open: ... --set-stream-path v020=<a file that exists>

stream path     v020  data/smd/stream-real.json -> data/smd/stream-moved.json
                recorded in the version's stream_path_history, so the change is visible
                to anyone who reads the promise. Nothing was re-measured by this call.
```

修完之后 `--campaign` 里那一行消失（因为列表是算出来的，不需要"重新记录一次修好了"），版本文件里留下
`stream_path_history: [{"from": "data/smd/stream-real.json", "to": "data/smd/stream-moved.json", "at": …, "why": …}]`。
随后 `--verify --verify-version 20 --stream-file data/smd/stream-moved.json --accept-copied` 直接在这份文件上跑，
不再出现 `(substituted: …)`：重测得到 `FAILED and reverted`，那是因为 40 窗口的合成流确实达不到承诺的 0.507，
和路径无关——记录修好之后，判定的对象终于是"这份文件上的承诺"。

三个实现缺陷（都是"判断句的对象不对"的同一个毛病）：

- 新行被塞进 clearing 的 gate（`if result.in_force_history and total:`），于是**没有任何清账时它不打印**——同一个
  形状在第 87 轮出现过（边界 184）。现在它在 gate 外面：记录指着打不开的文件，这件事和"有没有清过账"无关。
- `_stamp_moment` 返回 `datetime`（负责补时区），直接塞进 JSON 时 `TypeError: Object of type datetime is not JSON
  serializable`；这个文件里其他写入点都在边界处 `.isoformat()`，只有新代码忘了。
- 测试断言把标记顺序写反了：输出是 `… (in force), which …`，断言写的是 `… which … (in force)`。断言是照着想象
  写的句子，不是照着输出抄的句子。

**边界 200：修检查不等于修记录。** 替换能让一次检查跑完，记录仍指着打不开的文件；页面若要说"去修记录"，指的
必须是记录自己的字段，而不是它看见的那几次替换——那是调用者的习惯，改记录改不到。
**边界 201：修一份承诺要校验并且留痕。** 新路径必须指向一个能打开的文件（指着空处等于修好文书、丢掉证据），
旧值必须写进这份承诺自己的历史里；版本文件是承诺本身，除此之外没有别处记得它在哪份文件上量过。
**边界 202：一个事实的守卫要挂在它自己的主语上。** 记录是否可达和有没有清过账无关，把它塞进 clearing 的 gate，
就会在"全新检出、什么都没清过"时恰好沉默——这正是边界 184 说的那件事，这一轮又踩了一次。

### 3.100 修过的位置不是原来的位置

第 93 轮给了修记录的命令 `--set-stream-path`。它修好了一个洞（记录指着一份打不开的文件），同时悄悄地改了
一件更要紧的事：**这份承诺现在说的是另一份文件**。而这一轮之前，没有任何页面印出这件事——`--history` 只列版本、
清账和上限，`--campaign` 只数断链、补过的末端和打不开的路径。于是一条被人工搬过的记录，读起来和
"一开始就量在这份文件上"的记录一模一样。这和第 83 轮"补过的末端"是同一类事实，只是换了个字段。

做法和那一轮对齐：

1. `promote.repaired_paths(root)` 扫版本文件，列出 `stream_path_history` 非空的版本，返回**整条链**
   （`moves`）、`first_from`、`to`、`at` 与 `in_force`。返回整条链而不是一对值，是因为只有 `from`/`to` 时
   "搬了一次到这里"和"搬了三次、最后到这里"读起来一样（边界 204）。
2. `--history` 在清账链之前加一段：

   ```
   stream paths     1 record(s) have had their measured file rewritten by hand:
     v020  data/smd/stream-real.json -> data/smd/stream-moved.json  (1 move(s), last 2026-11-19T21:08:34.846728+00:00)
   ```

   并在 `--json-out` 里给出 `stream_path_moves`，这样脚本不必解析页面。
3. `--campaign` 数在 `dead_record_paths` 之前：

   ```
   links             1 record(s) have a rewritten stream path: v020 moved 1 time(s) -- the location is a repair,
                     not the original measurement, and `brian evolve --history` prints each move
   ```

两个页面都不缓存这个事实，各自从版本文件算——所以第 93 轮那条 `--set-stream-path` 会在下一次出的每一页上出现，
不需要任何人记得"把修过的事写下来"。

实测（`var/demo90/answerable`，第 93 轮留下的那条迁移）：`--history` 打出上面那一段，`--campaign` 打出上面那一行；
同时 `--campaign` 里已经没有"打不开的路径"那一行，因为迁移后的文件是存在的——两件事被清楚地分开，而不是互相
掩盖。

**边界 203：修过的位置不是原来的位置。** 一条被人工改过的记录必须自述被改过，否则它读起来和原始记录一样；
这是第 83 轮"补过的末端"换了个字段，同一条规矩。
**边界 204：一次修复要留整条链，而不是一对值。** 只有起止两点时，"搬了一次"和"搬了三次、最后到这里"读起来
一样，而它们的可信程度不同。
**边界 205：修记录不会让证据变新。** `--set-stream-path` 没有重测任何东西（命令自己就这么说），所以"路径被修过"
不能被读成"承诺被重新验证过"——前者是位置，后者是结论，两件不同的事。

### 3.101 在修过的位置上取的结论，也要自己说出来

第 94 轮把"这条记录的测量文件被人工改过"印到了版本表和 campaign 上。缺的是另一半：**检查**写下的
`verification` 什么也没说。于是在修过的位置上跑出来的结论，读起来和一开始就量在承诺所指文件上的结论一模一样——
这既是边界 203 的延伸（改过的记录必须自述被改过），也是边界 205 的落点（"路径被修过"不是"承诺被重新验证过"，
而"验证"这件事恰恰是在这里被写下来的）。

做法：

1. `Promotion` 带上第 93 轮自己写的字段 `stream_path_history`，并给出 `path_moves` / `path_origin` / `path_current`。
2. `--verify` 在**测量之前**说清站在哪里：

   ```
     v020  digest.rolling_window = 200  path rewritten 1 time(s) by hand: data/smd/stream-real.json -> data/smd/stream-moved.json
           this check measures the repaired location, not the file the promise first named (boundary 203)
   ```

3. 三条写 `verification` 的路径都记 `stream_path_moves`——包括定义缺失、只能写 `unmeasurable` 的那条，
   因为"站在哪个位置"是记录的事实，不是判决的产物（边界 207）。
4. 版本表在行尾标 `(path rewritten N time(s) by hand)`，并在判决后面标 `[measured on the repaired path]`：

   ```
   v020     digest.rolling_window = 20 recall 0.507 reverted …  [measured on the repaired path]  (path rewritten 1 time(s) by hand)
   ```

5. `repaired_paths` 多算一个 `checks_on_repair`（当前判决与 `checks` 历史里 `stream_path_moves > 0` 的条数），
   `--campaign` 把它和迁移数印在一起：

   ```
   links             1 record(s) have a rewritten stream path: v020 moved 1 time(s) -- …
   links             1 check(s) were taken after the repair: their verdict describes the repaired location, not the file the promise first named
   ```

实测（`var/demo95`，从第 93 轮迁移过的版本重建、只倒回部署那一半）：上面两段原样打出；`v020.json` 的
`verification` 里 `stream_path_moves == 1`、`verdict == "failed"`；`--history` 的 `stream paths` 段与第 94 轮一致。

**边界 206：** 在修过的位置上取的结论必须自己说出来。检查的结论是关于一个**位置**的；位置被人工换过而结论不自述，
读者会把"在 X 上验过"读成"在承诺最初指的文件上验过"。第 94 轮让**记录**自述，这一轮让**关于记录的判断**自述。
**边界 207：** 事实属于记录，不属于判决。即使这次检查跑不起来（定义缺失 → `unmeasurable`），`stream_path_moves`
也要写下来；否则"这次检查站在哪"取决于它是否成功，而失败的那一次恰恰最容易被误读成"在原位验过"。
**边界 208：** 计数要跟着被计数的东西走。`--campaign` 说"有 N 次检查是在修复之后取的"，而不是只把 N 条被修过的记录
念一遍——修两次只验一次，和修一次验一次，证据的成色不同。

### 3.102 补过一次的洞，可能自己长回去

第 93–95 轮把人工修复做成了一条完整的链：命令（`--set-stream-path`）、记录（`stream_path_history`）、
页面（`stream paths` 与 campaign）、以及**判断**（`stream_path_moves`、`[measured on the repaired path]`）。
缺的是最后一步：这个修复**可能已经不需要了**。如果原件重新出现在检出里，记录仍然指着替身，而页面只说
"路径被人工改过"——读者无法判断这条修复现在还需不需要，也无法知道有一个命令可以把它改回去。

这一轮让"原件回来了"成为一个可行动的事实：

1. `repaired_paths` 多报一个 `origin_exists`（原件在当前检出里是否存在）。
2. `--history` 在迁移行尾补一句：

   ```
     v020  data/smd/stream-real.json -> data/smd/stream-moved.json  (1 move(s), last …)  (the original file is back: data/smd/stream-real.json)
   ```

3. `--campaign` 点名记录、文件与命令：

   ```
   links  v020's original file data/smd/stream-real.json is back in this checkout: the record can be pointed at it
          again and measured there -- brian evolve --set-stream-path v020=data/smd/stream-real.json
   ```

   只告诉读者"原件回来了"而不给命令，等于只告诉他一个怀疑。

4. `--verify` 会分清自己站在哪里：用原件跑时记 `on_original_path` 并改说法

   ```
         this check measures the original file the promise first named, while the record still points at the repair (boundary 209)
   ```

   版本表相应地标 `[measured on the original path]`，与 `[measured on the repaired path]` 互斥——一次判决只站在
   一个位置上，两个标记同时出现是自相矛盾（这是本轮实现里先犯、后修的错）。

实测（`var/demo95`，把第 93 轮被改名的原件复制回来）：`--campaign` 打出上面那行；`--history` 打出
`(the original file is back: …)`；`--verify --stream-file data/smd/stream-real.json` 打出"原件"那句，
`v020.json` 的 `verification` 里 `stream_path_moves == 1`、`on_original_path == true`，版本表标
`[measured on the original path]` 且不再标 repaired。

**边界 209：** 修好的洞可能自己长回去。当原件重新出现时，页面必须点名这件事并给出把记录指回去的命令；
只说"修过"、不说"现在可以不改"，读者就无法判断这次修复还需不需要。
**边界 210：** 同一句判决在两种位置上含义不同。`failed` 在原件上跑出来的意思是"这个承诺不成立"，在替身上跑出来
可能是"替身与原件不同"——所以两种位置的判决必须能被分开读，而不是共用一个词和一个标记。

### 3.103 指回原件不是又一次修复：一次往返，以及"位置复原了、证据没有"

第 96 轮教会了页面：原件可能回来，并且给出了把记录指回去的命令。它没说清的是，运维真的执行那条命令之后会发生什么。

`--set-stream-path` 把这次回归记成了"又一次迁移"。于是 `--history` 打出第二条"迁移"，`--campaign` 仍然把这条记录描述成"在一个修复过的位置上"，而记录一旦指回原件，读起来就像那条承诺在原件上又被回答过一次——尽管它持有的判定几乎全部是在替代件上取到的（边界 210：同一个词在两个位置上是两件事）。

**改了什么。** 三处，都围绕"这条记录的旅程"：

1. `set_stream_path` 自己判断这是不是一次回归：读 `stream_path_history[0]["from"]`，如果目标等于它、且和当前值不同，`returned = True`，并把 `why` 写成"原名文件已回到这个检出里：这次迁移把承诺指回它"，同时在该条历史里写 `"return": True`。**理由不是省事**：调用者手写 `why` 时看不见记录的形状，只有命令自己知道"这是回归"还是"又迁了一次"（边界 211）。
2. `repaired_paths` 现在给出 `returned` / `returned_at` / `checked_after_return`，并且 `checks_on_repair` **不再把站在原件上的检查算作"在修复位置上取的判定"**——同一份判定不能同时属于两个位置。
3. `--history` 把整段旅程读成一行（`a -> b -> a  (round trip: 2 move(s), returned <时刻>)`），`--campaign` 则把"位置复原"和"证据没复原"分成两句：没有任何检查站在原件上时，它点名 `--verify` 那条命令；有检查站上去之后，它改成"a check has measured it there since the return (<时刻>)`。

**实测（`var/demo95`，记录已迁到 `data/smd/stream-moved.json`，原件 `data/smd/stream-real.json` 已恢复）。**

```
$ python -m brian.cli evolve --set-stream-path v020=data/smd/stream-real.json
stream path     v020  data/smd/stream-moved.json -> data/smd/stream-real.json
                recorded in the version's stream_path_history, so the change is visible
                to anyone who reads the promise. Nothing was re-measured by this call.
                this one is a return: the record is back on the file the promise was
                born with, while the evidence it holds was measured on the substitute.
```

`--history`：

```
stream paths     1 record(s) have had their measured file rewritten by hand:
  v020  data/smd/stream-real.json -> data/smd/stream-moved.json -> data/smd/stream-real.json  (round trip: 2 move(s), returned 2026-11-19T21:46:09.219326+00:00)
```

`--campaign`（还没有检查站在原件上）：

```
links             1 record(s) had their measured file rewritten by hand and are back on the file the promise was made on: v020 (round trip of 2 move(s)) -- the trip is closed, and what has been measured there since is stated below
links             v020's record is back on its original file data/smd/stream-real.json, but no check has stood there since the return: the location is restored, the evidence is not -- brian evolve --verify --verify-version 020 --stream-file data/smd/stream-real.json
```

照着那行跑一次之后（`FAILED and reverted`，判定写在原件上）：

```
links             v020's record is back on its original file data/smd/stream-real.json, and a check has measured it there since the return (2026-11-19T21:57:50+00:00)
```

**三个读数。**

1. **"指回去"和"改过一次"在记录里长得一样，除非命令自己说。** 第一次实现里 `--set-stream-path` 对往返和再迁移写同一句 `why`，`--history` 于是打出两条并列的"迁移"，读者只能靠时刻顺序去猜哪条是回程——而按时刻排序是推断，不是记录（边界 169 的老账）。
2. **位置复原 ≠ 证据复原。** 记录指回原件后，它历史里的 `verification` 仍是在替代件上取的；`--campaign` 如果只说"已经回到原位"，读起来就是"又重新回答过了"。所以那句话必须把两件事分开说，并且**在没有任何检查站上去时点名那条命令**（边界 185：诊断指名的命令必须真的能跑）。
3. **判定不能同时属于两个位置。** `checks_on_repair` 原本把任何带 `stream_path_moves` 的检查都算进去，包括站在原件上的那次；同一条判定被两边各数一次，读者就会以为替代件也被验证过。现在原件上的检查只进 `checked_after_return`。

### 3.104 一份记录的证据，要按"取在哪个文件上"分开数

第 96 轮分开了"量在修复位置上"和"量在原件上"，第 97 轮说清了一次往返的终点。仍然糊在一起的是**数量**：`stream_path_moves` 只说明某次检查站的位置不是记录最初的文件，却不说它站在哪个文件上。于是一旦来回走过，页面只能重复"1 check(s) were taken after the repair"——一个没有分母、也没有地点的计数，读者没法回答"这条承诺的证据里，有几份来自原件"。

**改了什么。**

1. `verification_evidence(payload)`：把记录持有的每一份判定逐条展开成 `{at, verdict, path, source}`。`source` 是三态：`written`（第 98 轮之后写的检查自己记了 `checked_path`）、`deduced`（老检查只记了当时的迁移次数，`stream_path_moves = k` 恰好意味着它站在第 k 次迁移的目标上——这是从记录里**精确推导**出来的，不是猜）、空（两边都推不出来，算"无法定位"）。
2. `evidence_split(evidence, origin)`：数出 `original / elsewhere / unplaced / total`，并列出 `elsewhere_files`。用 `elsewhere` 而不是 "the substitute"：一条记录可能经过好几个文件，数字能支撑的说法只有"不是原件"；无法定位的一律单列，不折进任何一边（边界 166）。
3. `--verify` 写入的 payload 多两个字段：`checked_path`（这次站在哪个文件上）与 `prior_evidence_paths`（**写之前**记录里已有哪些判定、各自在哪）。判定的句子于是自带前提，读者不必从版本文件的键序去还原时间线。
4. `--campaign` 每条有迁移的记录多一行：`v020 holds 2 verdict(s) on file: 1 on its original file, 1 on another file (data/smd/stream-moved.json), 0 it cannot place`。

**实测（`var/demo98`，在 `var/demo95` 的基础上注入一份只记了迁移次数的旧判定）。**

```
$ python -m brian.cli evolve --verify --verify-version 20 --stream-file data/smd/stream-real.json --accept-copied
...
v020  digest.rolling_window = 200  FAILED and reverted: promised recall 0.507 at the 10% budget, re-measured 0.060 (below 0.456)
```

写进 `v020.json` 的判定里：

```
checked_path: data/smd/stream-real.json
prior_evidence_paths: [{"at": "2026-11-19T21:57:50+00:00", "verdict": "failed",
                        "path": "data/smd/stream-real.json", "source": "deduced"}]
```

`--campaign`：

```
links             v020's record is back on its original file data/smd/stream-real.json, and a check has measured it there since the return (2026-11-19T22:12:38+00:00)
links             v020 holds 2 verdict(s) on file: 1 on its original file, 1 on another file (data/smd/stream-moved.json), 0 it cannot place
links             1 check(s) were taken after the repair: their verdict describes the repaired location, not the file the promise first named
```

**三个读数。**

1. **"N 次检查"不是证据的组成。** 同一个 N 可以是"全在原件上"也可以是"全在替代件上"，而这两种记录对读者的含义相反。所以数字必须带地点才有用，这就是这一轮的最小改动：让每份判定记下自己站的文件。
2. **推导出来的事实要自己说是推导的。** 老检查没有 `checked_path`，但 `stream_path_moves = k` 与 `stream_path_history` 一起**唯一确定**它站在哪——这不是猜测，是从记录里算出来的。可它与"检查自己写下"仍不是一回事：一个是当时的证词，一个是事后的算术（边界 187 的分野，挪一个字段）。所以 `source` 分开记，页面在需要时可以把两种混着数，但读者能看见比例。
3. **数不出来就说数不出来。** 既没有 `checked_path`、迁移次数又超出已知历史（比如记录被手改过）的判定，计入 `unplaced` 而不是归到最近的一边。把无法定位的折进任何一侧，都是让沉默替读者选答案（边界 166 的第二次应用）。

### 3.105 第一次在原件上量，不是又一次复查

第 98 轮把一份记录的证据按"取在哪个文件上"分开数了。分开之后，一个以前问不出来的问题出现了：**这份承诺，
到底有没有在它出生的那个文件上量过一次？**

一份记录可以这样走：原件消失 → 记录改指替代文件 → 在替代文件上量了两次 → 原件回来 → 记录指回原件。它持有
两条判断，而这两条里没有一条说明它自己所在的位置。页面上原本只有两句话可以用：「回到原位后又查过一次」和
「第一次在原件上查」。这两件事完全不同，而前者会把后者藏起来——它让一份从未在原件上测过的承诺看起来已经被
反复检验过了。

`--verify` 现在先看自己的前置证据够不够：

```
  v020  digest.rolling_window = 200  path rewritten 2 time(s) by hand and back on the original: data/smd/stream-real.json -> data/smd/stream-moved.json -> data/smd/stream-real.json
        this check measures the file the record names again after the trip back; the verdicts it already holds were taken on the substitute (boundary 212)
        this is the first verdict taken on the file the promise was born with: all 1 it holds were measured elsewhere (boundary 213)
  v020  digest.rolling_window = 200  backfilled end: round-new.json has no successor-side record of its own: the end was copied from the retirement, not written by the queue
        measured, not applied: the record's end was copied, so the deployment is left alone
  v020  digest.rolling_window = 200  FAILED and reverted: promised recall 0.507 at the 10% budget, re-measured 0.060 (below 0.456)
```

上面这一份是"零份原件侧判断"的情形（`evidence_split["original"] == 0`）。同一棵树换成"已经有一份原件侧判断"
的记录，第一句就必须改口：

```
        this check measures the file the record names again after the trip back; 1 of the 2 verdict(s) it already holds were taken here, 1 elsewhere (boundary 212)
```

这是本轮运行中抓到的第二个缺陷。第 97 轮那句「它已持有的判断都取在替代文件上」在当时是真的（那份记录只有一条
判断，且取在替代文件上），但它把**当时那棵树的事实写成了句子的断言**。第 99 轮一加入"原件侧判断"，同一句话就
变成了假话——而且它恰好出现在读者最需要分清两种判断的位置。修法是让句子跟着数字走：先看 `prior_split`，有原件
侧的就报个数，没有才说"都在别处"（边界 190：机制变了，句子和钉住句子的断言要一起动）。

`--campaign` 上的两句话也就分开了。零份原件侧判断的记录只有"位置复原了、证据没有"，并点名那条命令：

```
links  v020's record is back on its original file data/smd/stream-real.json, but no check has stood there since the return: the location is restored, the evidence is not -- brian evolve --verify --verify-version 020 --stream-file data/smd/stream-real.json
links  v020 holds 1 verdict(s) on file: 0 on its original file, 1 on another file (data/smd/stream-moved.json), 0 it cannot place
```

查到第一次之后：

```
links  v020's record is back on its original file data/smd/stream-real.json, and this was the first verdict ever taken there (2026-11-19T22:24:25+00:00)
links  v020 holds 2 verdict(s) on file: 1 on its original file, 1 on another file (data/smd/stream-moved.json), 0 it cannot place
```

还有第三种判断：**定位不了**的。`verification` 里既没有 `checked_path`，它的 `stream_path_moves` 又大于历史长度
（例如记录只移动过两次，它却写成 7），那它就是一条没有位置的判断。这类判断不能折进任何一边——折进"原件侧"
会让承诺显得被测过，折进"替代侧"会显得它站在别处；两种折法都是替一条说不清来源的证据选边（边界 166）。
页面现在按时间 + 判词逐条列出：

```
links  v020 holds a verdict it cannot place: 2026-12-01T12:00:00+00:00 (unmeasurable)
```

一条读不出位置判断的记录，读者第一件想做的事是打开那个时间点看看发生了什么；只给一个数字，等于把追查的入口
也一起省掉了。

**边界 215：读不出位置的判断，要带时间和判词逐条列出来，不能只留一个计数。** 计数告诉读者"有东西不对劲"，
不告诉他去哪儿看；而这类判断的存在本身就是因为当时的记录不完整，指望读者从别处补出它的位置是没有道理的。

**边界 216：证据全在别处，不等于证据在这里。** "第一次在承诺出生的地方量"和"回到原位后又量了一次"是两件事；
页面把两件事都叫"回到原位后的复查"，就把"这份承诺从未在它指名的位置被测过"这个事实藏掉了——而它恰恰是修复
这条路存在的理由。

**边界 217：一句量化过去证据的话，必须跟着分裂后的数字走。** 第 97 轮写死"它已持有的判断都取在替代文件上"，
在当时那棵树上为真；第 99 轮一有原件侧判断，这句话就成了假话，而且假在读者最需要分清两种判断的地方。断言
过去证据的句子要先查 `evidence_split`，数字变了句子跟着变（第 190 轮的规则用在句子的主语上）。

### 3.106 一条记录的分裂是事实，"有多少条"是另一个事实：把孤儿承诺排成队列

第 99 轮让 `--verify` 和 `--campaign` 会说：这一条判断是不是**第一次取在承诺出生的那个文件上**。那是一句关于
单条记录的话。读者读完之后，下一个问题一定是："这样的记录有几条？" 页面答不出来——它能说 v020 的分裂，说不出
账本上还有多少条承诺从未在出生地量过，也说不准先看哪一条。

`never_tested_at_origin(rows)` 把这件事做成数字：遍历 `repaired_paths` 的行，凡是 `evidence["original"] == 0`
的就算一条，按"第一次移动之前那个文件"分桶，并按版本号排好。两个细节：

- **"从未测过"包含"一次都没查过"。** 一份记录可能连一条判断都没有；只统计"证据全在别处"的记录会把这种最弱的
  记录从队列里漏掉，而队列的分母就因此变小了（第 79 轮：读不出状态的字段要算作自己的一类）。
- **出生文件不是当前文件。** 用的是 `first_from`（第一次移动的 `from`），不是 `stream_path`；记录的当前值是
  修复的结果，而队列问的是"这份承诺是在哪儿被许下的"。

`--campaign` 因此多了两句话。只有一条时：

```
links  1 record(s) hold no verdict on the file their promise was born with: v020 (1 verdict(s), all elsewhere) -- start with brian evolve --verify --verify-version 020 --stream-file data/smd/stream-real.json
```

出生文件有多个时，先按文件分开数，再点名第一条：

```
links  never tested at their origin: data/smd/third.json 1 record(s); data/smd/stream-real.json 1 record(s)
```

原件不在本地时不假装能测：

```
links  1 record(s) hold no verdict on the file their promise was born with: v020 (no verdict at all) -- the file it was born with is not in this checkout, so it cannot be measured there
```

`--verify` 也补一句话：当这次检查不会碰到承诺出生的地方、而那个文件还在时——

```
        this check measures the repaired location, not the file the promise first named (boundary 203)
        this promise has never been measured on the file it was born with (data/smd/stream-real.json), which exists here:
        measuring there is what would test the promise where it was made (boundary 218)
```

这句话只是提示，不换文件：换文件是调用者的事（第 92/197 轮：替换必须自己说出来，不能悄悄换）。它修的是另一种
沉默——在替代文件上再量一次、再失败一次，页面上会多一条"又检查过了"，而承诺本身从来没被碰过。

量在原件上的那一次，历史里也认得出：

```
clearings        1 recorded clearing(s); each names the measurement that earned it:
  v020  attempt 1  2026-11-19T23:13:23+00:00  failed        data/smd/stream-real.json  [first verdict at the origin]
```

`record_clearing` 因此多一个 `first_original` 字段，`--history` 的 `clearings` 块据此加标记。“第一次在原件上
量”是结账链上的一个事件，不该只活在版本文件的一个布尔字段里。

**边界 218：一次不测承诺的检查，要说清楚它没测。** 在替代文件上取的判决是"这份替代文件上的承诺不成立"，不是
"这份承诺不成立"；页面不说这一点，读者会把"又查了一次"当成"承诺被反复检验"。指针换不换由调用者决定，但沉默
不是选项。

**边界 219：一条记录的分裂要配一个总体计数。** 单条记录的 `0 on its original file` 回答不了"这算多吗"；账本
必须能给出队列的总量、按出生文件的分桶、以及第一条该打开哪一份——否则读者只能把它当成孤例。

**边界 220："从未在出生地量过"包含"一次都没量过"。** 只数"证据全在别处"的记录会让队列小看自己，而被漏掉的
恰恰是最弱的那几条（一条判断都没有的记录）；统计一个"从未做过的事"，要把"什么都没做"算进去。


### 3.107 把队列从一句话变成一个命令：`--from-queue` 与抄错的路径

第 100 轮让 `--campaign` 数出了"有多少条承诺从未在它出生的文件上被量过"，并且指名了第一条该跑的命令。可是跑那
条命令的方式，仍然是把版本号和路径从页面上抄下来——而"抄下来的路径"正是第 99 轮出问题的地方：`--verify` 会照
着调用者给的文件去量，抄错一个字符，这一量就落在了替换件上，页面于是把"在别处又查了一次"写成"在原件上查了新
的一次"。

所以这一轮把队列本身变成入口。`promote.origin_queue(root)` 是唯一的一份队列（`never_tested_at_origin` 套在
`repaired_paths` 上，按版本号排序），它同时供给三处：campaign 页面上的那句话、`--campaign --json-out` 的
`origin_queue`、以及 `--verify --from-queue N`。页面印的列表和命令跑的列表必须是同一份，否则"照着上面做"这句
话就又变成了手工活。

`--from-queue N` 的几条规矩，都是为了它不能悄悄改变问题：

- 它和 `--verify-withheld` 互斥（一个量的是"从未在原件上量过的承诺"，一个重测的是被驳回的候选），和
  `--verify-version` 也互斥（队列自己决定量哪几条）。两处都直接退出 2 并说明理由，而不是猜一个。
- 原件不在这个 checkout 里的记录只能计数，不能丢弃：页头写 `1 left out`，不然一次执行看起来比它实际做的更完整。
- 限制写成 `N of M runnable`，因为"我跑了 1 条"和"队列里有 3 条"是两件事。
- 每一条内部都用**记录自己的**原件路径，不用当前的 `stream_path`，也不用调用者给 `--stream-file`：这正是
  `--from-queue` 存在的理由。
- 多条检查只回一个退出码时取最坏的那个（2 比 1 比 0），否则调用者读到的"通过"会盖住后面的构建缺陷。

实跑（`var/demo100`，v020 的文件被手工改到 `data/smd/stream-moved.json`，只有一条取在替换件上的结论）：

```
from queue       1 of 1 runnable promise(s) hold no verdict on the file they were born with; measuring each on its own origin

---- v020  <-  data/smd/stream-real.json  (never measured where this promise was made)
verifying 1 promotion(s) from smd on SMD
  v020  digest.rolling_window = 200  path rewritten 1 time(s) by hand: data/smd/stream-real.json -> data/smd/stream-moved.json
        this check measures the original file the promise first named, while the record still points at the repair (boundary 209)
        this is the first verdict taken on the file the promise was born with: all 1 it holds were measured elsewhere (boundary 213)
  v020  digest.rolling_window = 200  backfilled end: round-new.json has no successor-side record of its own: the end was copied from the retirement, not written by the queue
        measured, not applied: the record's end was copied, so the deployment is left alone
  v020  digest.rolling_window = 200  FAILED and reverted: promised recall 0.507 at the 10% budget, re-measured 0.060 (below 0.456)

cleared         1 in-force observation(s) were answered by this check:
  smd              v020  backfilled end stood 20.0 day(s), answered on data/smd/stream-real.json
```

同一份队列也在 JSON 里（`--campaign --json-out queue.json`）：

```
origin_queue: {"records": 1, "by_file": {"data/smd/stream-real.json": 1},
               "versions": [{"version": 20, "origin": "data/smd/stream-real.json", "verdicts": 1, "origin_exists": true}]}
```

跑完之后同一个 `--campaign` 的 `origin_queue` 变成 `{"records": 0, ...}`——这是对的：这条承诺现在**在**原件上有
结论了，队列里不该还留着它。

**边界 221：一个指名了命令的队列，仍然要调用者重抄一个标识符，而抄下来的标识符就是替换件溜回来的地方——页面
印出的列表必须就是命令执行的列表。**
**边界 222：执行队列不能悄悄把它变宽或变窄：够不着的条目要计数而不是丢掉，上限要写成 `N of M`。**
**边界 223：多条检查只报一个退出码时，必须取最坏的那个，否则调用者读到的汇总会盖住构建缺陷。**


### 3.108 同一句结论，两种来路：清账要说出它是不是队列跑出来的

第 101 轮让队列可以被执行。这带来一个此前无法表达的事实：这笔清账是**队列**跑出来的，还是**人手抄**着版本号和路径
跑出来的？结论一模一样，差别在于有没有人重抄过一个标识符——而那正是替换件溜回来的地方（边界 221）。所以这一轮让
清账记录自己的来路。

`record_clearing(..., queued=)` 把 `queued` 写进条目：`True` 表示 `--from-queue` 挑的这条，`False` 表示调用者自己
写下了版本和文件，字段**缺失**表示这条记录写在这个字段存在之前。三种状态分开数，因为把缺失折进任何一边都是让默认值
替读者作答（第 79 轮的规则）。

`--history` 在对应行尾加 `[from the queue]`，`--campaign` 多一句：

```
links             1 clearing(s) were produced by the queue, 0 by a hand-typed command
```

人手跑一次，同一句变成：

```
links             0 clearing(s) were produced by the queue, 1 by a hand-typed command
```

**这句在没有任何队列清账时也要印**。第一版只在"队列跑过"或"有老记录没写字段"时印，于是全手抄的账本上这一行干脆
不存在——而缺失的一行会被读成"没有这个事实"，不是"没有一条来自队列"（边界 162 又出现一次，这次出现在一个"分列"
里）。

另外，`--from-queue 0` 现在只**列出**队列，不量任何东西：

```
from queue       1 of 1 runnable promise(s) hold no verdict on the file they were born with; --from-queue 0 only lists them
  v020  <-  data/smd/stream-real.json  (never measured where this promise was made)

nothing was measured. Run the same command with --from-queue N to measure the first N of them on the files they were born with.
```

实跑（`var/demo100`）：`--from-queue 1 --accept-copied` 之后，`--history` 的 clearing 行是

```
v020  attempt 1  2026-11-19T23:43:50+00:00  failed  data/smd/stream-real.json  [first verdict at the origin]  [from the queue]
```

而同一份账本改用 `--verify-version 20 --stream-file …` 手抄跑一遍，行尾只有 `[first verdict at the origin]`，两个标记
是两件独立的事，所以它们各印各的。

**这里踩到一个"0 被当成假"的坑**：`from_queue` 既是"是否进入队列模式"又是"跑几条"，而 `0` 在 Python 里是假值。第一版
用 `if from_queue:` 守这个分支，于是 `--from-queue 0` 直接跳过整个队列段，落进普通 `--verify`（没有版本、没有文件），
退出码 1。修法是把"是否在场"和"取什么值"分开：`from_queued is not None` 才进入队列段，`int(from_queued or 0)` 才是
上限。测试里那条 `--from-queue 0` 的用例正是靠这个错误才写出来的。

**边界 224：结论不会因为命令是手打的还是队列跑的就变，但"有没有人重抄过标识符"是另一个事实，而它恰恰预测替换——
所以必须记下来，否则"同一条结论"会把替换的来路抹平。**
**边界 225：计划和执行必须可分辨：`--from-queue 0` 只列不量，而"没有给值"不能被读成 0——一个同时表示"是否进入"和
"跑几条"的参数，必须用在场的标志和数值本身分开判断。**
**边界 226：一个分列必须把零印出来：全手抄的账本要写"0 … 1"，因为缺行会被读成"没有这个事实"而不是"没有一条来自
队列"。**


### 3.109 走一趟也要留记录：队列执行本身是一条事实，以及遇到断账就停

第 102 轮之后，页面已经能说"哪些承诺还没有在它出生的文件上被量过"、也能说"每一笔清账是队列跑出来的还是手打命令
跑出来的"。但它说不出**上一次有人走队列是什么时候**。一个从来没被排空过的账本，和一个一小时前排空过的账本，印
在纸上是同一张干净的页面——"队列空了"和"没人走过队列"是两种状态，只有一种是好消息。

所以走队列这件事本身要落一条记录：`var/evolve/queue_runs.json`，格式 `brian.queue/1`，保留最近 20 条，被裁掉
的条数单独记（`trimmed`），因为"上一次执行"不等于"全部历史"。一条记录里是读者事后无法重建的东西：

```
{
  "at": "2026-11-19T23:59:38.547122+00:00",
  "limit": 1, "runnable": 1, "measured": 1,
  "skipped": 0, "skipped_origins": [],
  "cleared": 1, "worst": 1, "stopped_at": 0
}
```

`measured` 与 `cleared` 是两个数，不是同一个数的两种说法：一次检查可以跑完却没有settle 任何东西（第 89 轮的边界
188 再往上抬一层）。把两者并成一个，就会把"量了三条"读成"答掉了三笔"。`cleared` 是执行前后各读一次观测账本、
只数这一趟带 `queued` 标记新写下的条目得到的，不是从"量了几条"推出来的。

页面随即能说了：

```
links             the last queue run was 2026-11-19T23:59:38.547122+00:00: measured 1 of 1 runnable promise(s) on their origins, left 0 out, and 1 of those measurements left a clearing
links             the last queue run was 2026-11-20T00:04:15.361779+00:00: measured 1 of 1 runnable promise(s) on their origins, left 0 out
links             it stopped at v020, which exited 2: a build defect is not something the next promise in the queue measures away
```

第二段来自一个把 `v020` 的产出轮次放进 `mismatch` 链接的账本。走到它时 `--verify` 拒绝在断账上叠承诺（第 84 轮），
退出 2。走队列此时必须停下：退出 2 的意思是**这个记录的账目互相矛盾**，而队列里的下一条承诺并不能把矛盾量没。
于是记录里写 `stopped_at: 20`、`measured: 1`、`worst: 2`，页面点名停在哪一条、后面还剩几条；只有一条时不印
"0 条留在队列里"，而是说没有人在后面排队（边界 226 的老规则，换了一行）：

```
stopped          v020 exited 2, and no other promise was waiting behind it: a contradiction is not something the next promise measures away
1 promise(s) measured on the file they were born with. A copied end is measured, never applied; a contradiction is still refused.
```

**两个实跑才暴露的问题。** 其一，`--from-queue` 只被 `--verify` 分支读，漏掉 `--verify` 时它整个被忽略，命令转而
量了一整轮（记录落在 `round-*.json` 而不是队列记录里，还没人看得出来）。这类"只被一个模式读取的开关"必须拒绝被其
他模式静默忽略，于是加了一个守卫：缺 `--verify` 直接退出 2 并说明原因。其二，第一版停住时无论后面有没有东西都印
`0 promise(s) were left in the queue: none`——零个对象也要印出来是第 226 轮的要求，但"没有一个在等"不是零个对象的
事，是一句不同的话，所以分成两支。

边界：**227**（一个会列出队列的页面必须同时说队列上一次是什么时候被走的——"没有承诺在等"和"没人走过队列"是两种
状态，只有一种是好消息）／**228**（一次走队列以断账收场时必须停下并点名没量的那些：退出 2 说的是账目矛盾，而
"再量一条"不可能把矛盾量没，所以继续走只会把同一个缺陷重复收费）／**229**（一次走执行要分开记"量了几条"与
"答掉几笔"——检查可以跑完而不 settle 任何东西，两个数并成一个就把执行读成了成效）。


### 3.110 同一笔账不要付两次：把走过的路当历史，把被账本挡住的记录标出来

第 103 轮让每一趟队列都留下一条记录，于是"队列什么时候走过"有了答案；这一轮要答的是另外两个问题：
**走过几趟、都量出了什么**，以及**同一笔账会不会被重复付**。

一趟走完，`var/evolve/queue_runs.json` 里多一行：

```
2 queue run(s) recorded: measured 2 promise(s) in total, 0 of those left a clearing,
2 run(s) ended on a build defect (1.0 promise(s) per run on average)
```

只有一趟时不打这句：一趟是事实，不是方向；把一趟说成"趋势"，等于把一次观测当成一条规律
（第 60 轮的教训，换个地方）。

**同一笔账不要付两次。** 第 103 轮的那趟队列停在了 `v020` 上，因为它的记录参与一条 `mismatch`
链接——账本自己都不同意，重新量一遍只会得到同一个结论。于是下一趟走过来时，页面先说：

```
still blocked    1 promise(s) ended on a build defect last time and nothing has changed since: v020 (…)
                 fix the books, or pass --retry-blocked to measure them again anyway
no promise in the queue is left to measure: 1 record(s) still owe a check where they were made,
                 and the last walk's refusal still stands (fix the books, or pass --retry-blocked).
```

`blocked_versions(root)` 从走法记录里读出"上一趟试过、并且以 exit 2 结束"的版本；`--retry-blocked`
是绕过它的唯一方式——绕过不是默认，因为重复测量一份矛盾的账本既花 Laya 的时间，也不会改变结论。

这里有一处判断被实跑推翻：第一版的 `not runnable` 分支只有一句话，于是**所有记录都被挡住**时，页面说的是

```
no promise in the queue can be measured here: … none of those files is in this checkout
```

——原件明明就在那里。两个不同的原因共用一个句子，读者就会去找不存在的问题。现在两条分支各自说话。

**一趟什么都没量也是走了一趟。** 第二版把"没有可量的记录"当成早退，直接 `return 0`，不留记录；
结果卡住的队列在 `--campaign` 上看起来像**没人来看过**。现在这种情况也写一行
`{"measured": 0, "runnable": 0, "skipped": …, "skipped_origins": …}`，趋势因此能显示
"反复来、反复量为零"：

```
2 queue run(s) recorded: measured 0 promise(s) in total, 0 of those left a clearing, 0 run(s) ended on a build defect
origins skipped on more than one walk: data/smd/stream-real.json 2
```

**边界 230：只记"量了几条"不记"量了哪几条"的记录，分不清"没试过"和"试过、被拒了"。**
`attempted: [{"version": 20, "code": 2}]` 才是把计数变成历史的那一项。

**边界 231：趋势必须说明窗口。** 存储只留最近 20 趟，`trimmed` 数出掉出去的趟数；不写这句话，
"每趟量 1.0 条"就是在为一段它已经不再持有的历史报数。

**边界 232：记在某条承诺上的拒绝，必须能被"修好账本"清掉，而绕过它的命令必须写在这条拒绝旁边。**
被拒的记录不是被禁止测量，而是"在账本自相矛盾时再量没有意义"；读者要知道两条出路，否则这条拒绝
就变成一张永久的封条。


### 3.111 拒绝也要点名：被挡住的承诺，说出是哪本书挡住它的

第 104 轮让被账本挡住的承诺不必重付一次，但它留下的记录只有 `{"version": 20, "code": 2}`——
**拒绝了，却没说谁拒绝的**。读者看到"账本不同意"，手里没有任何一条记录可以打开。

于是这一轮给拒绝补两样东西。

**一、理由。** `blocked_versions` 的每一行多一个 `why`，直接取自 `provenance_block` 早就知道的那句话：

```
blocked by the books: data/smd/stream-real.json 1 promise(s) (v020), refused 1 time(s), last 2026-11-20T00:26:43 -- round-old.json is one of the records in a mismatch link: round-old.json names round-new.json as its successor, but round-new.json says it replaced round-other.json
```

一行里有文件、有被挡的版本、有被挡次数、有那句话里点到的两条记录。

**二、按原件分桶。** `blocked_dossier` 把被挡的版本按**出生时的文件**（`first_from`）分组，而不是按当前
路径：挡住它的是记录本身，而原件才是读者要打开去修的那一份。同一份文件下的承诺数、被挡总次数、最后一次
时间、以及那些理由，一起给出。

**长句子印一次，短标记印在行上。** 第一版把整句矛盾塞进 `--history` 的每一行，结果那三行全部超过两百列——
三个应用过 `v020` 的轮次各自重复同一个长句，把本来要看的列挤掉了。现在的分工是：

- `--history` 的行只写 `[refused by the queue on 2026-11-20T00:26:43]`：**这个承诺被队列试过、被拒了、什么时候**；
- `--campaign` 在它所属的那份文件上写一次长句：**谁的书和谁矛盾**。

**顺便修掉一处重复。** 同一行可能被两条 finding 同时点名（每个"提名者"各自算一次冒犯），于是
`[broken link: mismatch with round-new.json, round-other.json]` 在同一行印了两遍。两条句子一模一样时只印一条：
行说"这里有一次矛盾"，表头说"有几次"。

**边界 233：拒绝记录若只有代码没有名字，读者拿到的是"有问题"三个字。** `code == 2` 说明是 build defect，
不说明是哪条记录；名册（`attempted`）解决了"付不付第二次"，理由（`why`）才解决"去哪里修"。

**边界 234：一条记录可以因多个来源被点名，但同一句话在同一行只该出现一次。** 重复不是更强调，是更含糊——
读者会以为是两处矛盾。

**边界 235：长句印在它所属的对象上，短标记印在引用它的行上。** 同一句话在一页里出现四次，代价不是四次
字符，是那一页的列宽。

实跑（`var/demo103`：`v020` 的生产记录参与一条 `mismatch` 链接）：

```
stopped          v020 exited 2, and no other promise was waiting behind it
links             blocked by the books: data/smd/stream-real.json 1 promise(s) (v020), refused 1 time(s), last …
 1  …  [retired, re-measured as round-new.json]  [data/smd/stream-one.json]  [broken link: mismatch with round-new.json, round-other.json]  [refused by the queue on 2026-11-20T00:26:43.501609+00:00]
```


### 3.112 账本修好了，拒绝就该自己失效

第 105 轮让每一次拒绝都说出了挡住它的那句话，那句话却是**每次读的时候现算的**：`--retry-blocked` 因此成了唯一的
出口，哪怕调用者刚刚把账本修好、现算的句子已经变了。第 106 轮把"拒绝的原因"存进走过的那一趟，并让页面和队列都
只认今天的那句话。

三类数字先分开（边界 236）。第 105 轮把一次阻截记成 `{"version": 20, "code": 2}`，再在页面上汇总成一个 `attempts`：
"同一个文件被挡了三次"和"这个文件上三条承诺各被挡一次"读起来一模一样，而前者要修的地方和后者完全不同。现在
`attempted` 每次记 `{"version", "code", "why"}`，档案里多出 `retried` 一份名单：

```
links             blocked by the books: data/smd/stream-real.json 1 promise(s) (v020), 2 refusal(s) across 1 retried promise(s), last 2026-11-20T00:37:28.706167+00:00 -- round-old.json is one of the records in a mismatch link: …
```

然后是那句原因（边界 237）。它必须被写下来，不能现算：一份只存 `code` 的账簿永远发现不了"书已经换了"。

```
links             the refusal of v020 no longer stands: the ledger has changed since 2026-11-20T00:37:28.706167+00:00
```

于是被挡住的承诺不必再借 `--retry-blocked`（边界 238）——队列自己把它接回来：

```
from queue       1 of 1 runnable promise(s) hold no verdict on the file they were born with; measuring each on its own origin
---- v020  <-  data/smd/stream-real.json  (never measured where this promise was made)
        this is the first verdict taken on the file the promise was born with: all 1 it holds were measured elsewhere (boundary 213)
  v020  digest.rolling_window = 200  FAILED and reverted: promised recall 0.507 at the 10% budget, re-measured 0.060 (below 0.456)
```

第四个缺陷是在**跑**的时候才出现的，也是这一轮真正的一课：判断"变了没有"的第一版要求两份句子都非空
（`bool(stored and current and stored != current)`）。可是把账本**彻底**修好之后 `provenance_block` 返回 `None`，
今天没有句子了——于是最干净的那种修复反而不算"变了"，被挡的承诺永远挡着，`--retry-blocked` 又成了唯一的路。
现场证据是 `provenance_block(root, 20) is None` 而 `blocked_versions` 仍拒绝 v020、`changed: False`。改成
`bool(stored) and stored != current`：**"今天没有这句话"是最强的变化**，不是没有变化。

第五个缺陷同源：同一份拒绝被三趟走各记了一次，页面就把同一句"拒绝不再成立"印了三遍，配三个时间戳，读起来像三件事。
`stale_refusals` 现在按新到旧去重，一条承诺一行（和这个存储里其他每个列表一致）。

**边界 236**：同一个文件被挡三次、和三条承诺各被挡一次，是两种故障；一个合计把它们读成同一种。

**边界 237**：拒绝必须记下当时拒绝它的那句话，否则"书变了"永远无法被发现。

**边界 238**：账本自己已经答掉的拒绝，必须在没有任何绕行开关的情况下失效；否则 `--retry-blocked` 变成常规入口，
而常规入口就是没有人去量过的那个入口。

### 3.113 一条被挡过的承诺，值得一份拒绝史

第 105 轮让 `--history` 的行带上 `[refused by the queue on <at>]`，第 106 轮让修好的账本自己解掉拒绝。两次都只回答
"**现在**还被挡着吗"——一个被挡过两次、随后修好、再没被挡过的承诺，和一个一小时前刚被挡的承诺，在页面上长得一模一样。

第 107 轮把存下来的那几趟走当成一份史来读：`refusal_history()` 扫一遍 `queue_runs.json` 里的每一趟，按承诺汇总
"被挡了几趟、第一次、最后一次、现在还成不成立"。

```
refusals          v020 was refused by the queue 1 time(s) among 1 recorded walk(s): first 2026-11-20T00:56:51.776676+00:00, last 2026-11-20T00:56:51.776676+00:00, and the refusal still stands
```

修好之后，同一行改口，而拒绝本身不会被删掉——那一趟确实走过：

```
refusals          v020 was refused by the queue 2 time(s) among 4 recorded walk(s): first 2026-11-20T00:34:56.791204+00:00, last 2026-11-20T00:37:28.706167+00:00, and it no longer stands
```

`--campaign` 用同一份数字，只是把"第几趟"说成一句话：

```
links             v020 was refused by the queue 2 time(s) among 4 recorded walk(s): first …, last …, and it no longer stands
links             blocked by the books: data/smd/stream-real.json 1 promise(s) (v020), 1 refusal(s) across none retried, last … -- round-old.json is one of the records in a mismatch link: …
```

三个缺陷都是**跑**出来的，而且一个比一个更说明"计数"这件事本身。

第一个：**"第一次"印成了最新的那次**。扫描是从新到旧（这个存储里所有列表都是新在前），`first_at` 就在第一次遇见时
被赋值，于是印出来的"first"是最近的那趟。第一版实测 `first 00:37 … last 00:34`，顺序反了——读者会以为这条承诺
刚刚才被挡。现在把时间戳收齐再排：`first` 是最旧、`last` 最新。

第二个：**同一句长标记印在每一行**。一条承诺可能由三条记录产出（`applied` 都含 `v020`），三轮的 `round-other`/`round-old`/
`round-new` 各自成行，于是同一句"被挡过 2 次、第一次、最后一次"在页面上出现三遍，三行都被撑到两百列开外。这正是
第 105 轮的边界 235：长句子属于它描述的那个对象，行只留短事实。现在行上是 `[refused by the queue, still standing]`，
计数与时间戳归一行表头：

```
refusals          v020 was refused by the queue 1 time(s) among 1 recorded walk(s): first …, last …, and the refusal still stands
```

第三个：**拒绝已经失效的承诺，行上什么都不印了**。第一版的行循环遍历的是 `blocked_rows`（现在还成立的拒绝），
所以"拒绝已失效"这条史只出现在表头、不出现在它自己的行上——而"为什么这一轮从没被量过"的读者恰恰是在找那一行。
循环改为遍历 `refusal_rows`，行上仍印短标记，表头说明它已不成立。

**边界 239**：从一个会被裁剪的存储里算出来的历史，必须同时说出它覆盖了几趟走（`among N recorded walk(s)`），
否则一个窗口里的计数会被读成有史以来的计数。

**边界 240**："第一次"和"最后一次"必须按时间排，不能按扫描顺序——日志是新在前读的，把最新的时间戳印在"第一次"
这个词下面，读者读到的频率会翻倍。

**边界 241**：同一条事实在每一行上重复，不是强调而是噪声；一条承诺由多条记录产出时，计数属于表头，行只留短标记。
这是边界 235 的第二个字段：上一轮是"哪本书挡的"，这一轮是"挡了几次"。

### 3.114 第几趟，比几点几分更有用：拒绝史要用趟号说话

第 107 轮把拒绝做成了历史，但每一条都只带时间戳：

```
refusals          v020 was refused by the queue 2 time(s) among 4 recorded walk(s):
                  first 2026-11-20T00:34:56.791204+00:00, last 2026-11-20T00:37:12.104887+00:00
```

读者拿到这一行，没有任何办法把它对上 `var/evolve/queue_runs.json` 里的某一条。时间戳只能和时间戳匹配，而一趟
走访在存储里是有位置的：第几趟、一共几趟。第 108 轮把位置写进句子：

```
refusals          v020 was refused by the queue 2 time(s) among 2 recorded walk(s):
                  walk 1 to 2, first 2026-11-20T01:04:44.307262+00:00, last 2026-11-20T01:06:16.776565+00:00,
                  and the refusal still stands
refusals          the last walk (2 of 2) refused v020
```

第二行是另一件事实：版本的拒绝史说"它被拒过几次"，最后一趟说"队列被留在什么状态"。一句属于版本，一句属于队列，
所以分成两行。`latest_refusals(root)` 只读最新一条 run 的 `attempted`，空队列返回
`{"walk": 0, "walks": 0, "at": "", "versions": []}`——"没走过"和"走过但没拒人"是两种状态，不能都印成空白
（对应 162 号边界）。

**同一个总数，两种事实。** 第 105 轮的档案只数"现在还被挡着的承诺"，于是"这个文件名下被拒了几次"没有行。补上
`refused` 之后又出现一次误读：先把 `(walk 3 of 3 count this file's promises)` 里的 "of" 写成拒绝次数，于是同一行
同时印出 `walk 3 of 2`——一个窗口序号配一个拒绝计数。修法是给档案条目补 `walks`（名下各承诺的窗口取最大），句子
变成：

```
blocked by the books: data/smd/stream-real.json 1 promise(s) (v020), 2 refusal(s) across 1 retried
promise(s), last 2026-11-20T01:06:16.776565+00:00 (walk 2 of 2; its promises were refused 2 time(s) in
the recorded walks)
```

一句里同时有"几趟里第几趟"和"这个名字下被拒了几次"，因为读者要判断的是花不花一轮去修账本，而这两件事一起才
构得成判断。

**边界 242：一份用时间戳命名事件的历史，读者打不开；事件在它窗口里的序号，才是可定位的。**

**边界 243：一个承诺被拒三次和一个文件下三个承诺各被拒一次，是不同的故障；总数必须同时给出计数和窗口，否则两
种情况印成同一句。**


### 3.115 两份记录要能对上同一个趟：拒绝史、文件的账、清单

第 108 轮让拒绝史用趟号说话，也给了每个原始文件一份"账"，但三处各说各的：历史里写 `walk 1 to 2`，档案里写
`refused 2 time(s)`，清单里干脆只有一句"ended on a build defect last time"。三个数字都对，读者却没法把它们
对上同一趟。

第 109 轮把趟号本身带出来：`refusal_history()` 每行多一个 `refused_walks`（去重排序的趟号列表），
`blocked_dossier()` 把名下各承诺的趟号合并，`walks_phrase()` 负责把它写成一句人话——`[3]` 是 `walk 3`，`[1, 2]`
是 `walks 1 and 2`，`[1, 2, 4]` 是 `walks 1, 2 and 4`。三处于是引用同一串数字：

```
blocked by the books: data/smd/stream-real.json 1 promise(s) (v020), 2 refusal(s) across 1 retried
promise(s), last 2026-11-20T01:06:16.776565+00:00 (walk 2 of 3; its promises were refused in walks 1 and
2, 2 time(s)) -- round-old.json is one of the records in a mismatch link: …
```

清单页也终于能自己回答"值不值得花一轮"：

```
still blocked    1 promise(s) ended on a build defect last time and nothing has changed since:
                 v020 (refused in walks 1 and 2, 2 time(s), last at 2026-11-20T01:06:16.776565+00:00)
                 fix the books, or pass --retry-blocked to measure them again anyway
```

`--retry-blocked` 让这条承诺重新进入清单时，行末再带一句结论：

```
  v020  <-  data/smd/stream-real.json  (never measured where this promise was made)
  [refused by the books in walks 1 and 2, and the refusal still stands]
```

**一处实现上的取舍**：`still blocked` 与清单行的措辞都要读历史，两处各查一次会给出两份可能不一致的结论，所以
`--from-queue` 分支只加载一次 `refusal_history()`（`walked_history`），两处共用一个 `_refusal_phrase()`，
"第几趟"这件事在整页上只有一份来源。

**边界 244：一处事实被两页引用时，两页必须引用同一个出处——趟号列表而不是各自的计数，否则三个数字都正确、
读者仍然对不上。**

**边界 245：清单页要能独立回答"值不值得修"；把一个判断拆到两个页面上，读者就得先知道要去看第二个页面。**


### 3.116 代价要写成两件事的乘积，顺序要写成一句话

第 109 轮把三处的趟号对齐了，档案行就此能说清楚"这个文件被拒了几趟"。但读者紧接着要问的是另一件事：**先修哪
一个**。档案按文件名排，那是字母顺序，不是优先级。

第 110 轮把它算成一个可执行的判断。两个数字都来自已经记下的账：

- `walks_blocked`：这支队列为它花掉了几趟（`refused_walks` 的长度）；
- `held`：名下还有几条承诺从未在原件上量过（它们正排在这条拒绝后面）。

代价是两者相乘（`held` 为 0 时按 1 算，因为花掉的趟数是沉没成本，不是零）：

```
repair order     data/smd/stream-real.json costs 2 walk(s) of refusals and holds 1 promise(s) behind it
                 -- start there (brian evolve --audit)
```

排序依次按代价、趟数、文件名。最后一个键不是洁癖：一个每隔两次运行就换一次顺序的页面，教读者不要相信它。

`--campaign` 的对应行是 `repair next: …`，脚本读的是 JSON：

```json
{"origin": "data/smd/stream-real.json", "records": [20], "walks_blocked": 2, "walks": 4,
 "last_walk": 2, "held": 1, "cost": 2, "remedy": "brian evolve --audit"}
```

**一个实现上的坑**：`repair_plan()` 的排序测试不能靠端到端搭两个被挡的文件——那要两套互不矛盾的错账。改成把
`blocked_dossier` 与 `origin_queue` 换成桩，断言排序规则本身（代价相同、趟数相同 → 按名字），端到端只测一行的
字段与两页的句子。测规则的地方测规则，测集成的地方测集成。

**边界 246：代价必须是它相乘的那两件事的乘积，并被写出来；"被拒 2 次"不能给两个文件排序。**

**边界 247：说出故障的页面必须说出第一个动作，而且要排序；一份没有顺序的清单，是让读者自己重算一遍。**


### 3.117 修完之后，要能说出它动了表没有

第 110 轮给了"先修哪个文件"的排序，代价是 `walks_blocked × max(1, held)`。这一轮的问题是：修完之后呢？排序是从账本
每次现算的，所以它自己答不了"上一次那个修复到底帮没帮上忙"——两份读数只在修复发生的那一刻同时存在。所以对比必须
当场记下来（边界 248），并且写进两份账：`var/evolve/repairs.json` 与 `repair_history()`。

```
$ python -m brian.cli evolve --set-stream-path v020=data/smd/stream-real.json
stream path     v020  data/smd/stream-moved.json -> data/smd/stream-real.json
                this one is a return: the record is back on the file the promise was
                born with, while the evidence it holds was measured on the substitute.
repair plan      nothing left the table: cost 1 -> 1, 1 file(s) still owing

$ python -m brian.cli evolve --campaign
links             repair next: data/smd/stream-real.json -- 1 walk(s) of refusals, 1 promise(s) held behind it (brian evolve --audit)
links             the last repair (2026-11-20T01:26:30.115979+00:00, set-stream-path) changed nothing in the plan: cost 1 -> 1, 1 file(s) still owing
```

"什么也没离开这张表"是一句结论，不是沉默（第 102 轮的零要打出来的规矩）。写入的记录是
`{"at", "action", "before", "after", "freed", "cost_fell"}`；`freed` 只装"修复后不再需要修的文件"，一个仍然在表里、
只是代价变低的文件不算解放——那是两件事，混在一起读者会以为它已经好了。

这一轮的实测结果，值得如实写下来：两条修复命令都没有让任何一个文件离开这张表，连一次都没有。原因是清楚的——
队列的拒绝来自 `replacement_links` 的**矛盾**（mismatch / unretired / dangling），而 `--repair-links` 只补
**legacy** 那一端（第 83 轮），`--set-stream-path` 改的是记录里的路径。它们修的是另外的书：

```
$ python -m brian.cli evolve --repair-links --apply
no legacy link: every retired round either has a successor that named it back, or none
$ python -m brian.cli evolve --set-stream-path v020=data/smd/stream-moved.json
stream path     v020 already says data/smd/stream-moved.json; nothing to change
```

所以第 110 轮那句 `remedy: brian evolve --audit` 说的是实话：**账本矛盾只有诊断，没有可执行的修法**。这正是这一轮
最有用的产出——记录机制一落地，第一件报告出来的事就是"你给的第一动作修不好它"（边界 249、250）。下一项因此是把
这件事补上：一条能明确判定继承人的命令，让表能真的空出来。

**边界 248：两份读数只在动作发生的那一刻同时存在，所以对比必须当场记下来；一个每次从账本现算的计划，说得出"该修哪个"，说不出"上次修了有没有用"。**
**边界 249：修复的结论要用它自己的两个数字（代价、还有几个文件欠着），并且"什么也没变"也要打出来——一次看不见效果的修复，和一次没跑的修复，在页面上长得一样。**
**边界 250：一个只列诊断命令作为补救的计划，等于没有补救；机制落地后报告的第一件事往往是"给你的第一动作修不好它"，那不是失败，那是这一层账本该干的事。**


### 3.118 矛盾只能由人裁定：一次结算是一条决定，不是第二个证人

第 111 轮之后，`--campaign` 会按代价排出一个"先修哪本书"的顺序，而那个顺序里的 `remedy` 只有一个诊断命令
（`brian evolve --audit`）。真跑一遍才发现原因：拒绝来自 `replacement_links` 的 `mismatch`——两条记录各自声称
同一个后继（`retired.re_measured_as` 与 `queue.paid_round` 互相矛盾）。这不是测量能解决的问题：两条声称里没有
第三条证据，只有人能说"这一条已经过时"。之前只能手工改记录文件，而手工改记录既不留决定者也不留理由，下一次
`--audit` 会把改动当成原始事实读（正是第 83 轮 `backfilled` 那一类）。

第 112 轮把"裁定"做成一条记录：

```
brian evolve --settle-link round-old.json=round-new.json --settle-why "队列付的是 round-other.json"
settled         round-old.json -> round-new.json
                recorded in var/evolve/settlements.json: this is a decision, not a measurement,
                and `--audit` prints it apart from the links that agree by checking.
                because: 队列付的是 round-other.json
repair plan      1 file(s) off the table: data/smd/stream-real.json -- cost 1 -> 0
```

`var/evolve/settlements.json` 记 `{record, successor, at, why}`；重复执行是幂等的（`changed: false`，并说出原本的
时间）。结算只做一件事：让**那一个声称者的声称**退出 `replacement_links` 的候选列表。它不参与"猜哪个对"——所以
`round-new.json` 的 `queue.paid_round` 仍是 `round-other.json`，账本本身一个字也没改。`--audit` 把结算单独打印：

```
> 1 replacement link(s) were settled by a recorded decision, not by agreement:
    round-old.json             -> round-new.json              (2026-11-20T01:37:35.544754+00:00)
> A settlement says who decided; it is not a second witness. `--audit` no longer charges these pairs,
```

`--strict` 随之从 2 回到 0，`--verify` 也不再因这条断链而拒绝叠承诺（`provenance_block` 读同一份结算）。

### 同一个账本，两页不能读成两件事

第一次跑完演示就露了一个缺陷：`--audit` 说"已结算、不再收费"，`--history` 仍打印
`2 replacement link(s) disagree with their other end` 并在行上标 `[broken link: …]`，`--strict` 却已经 0。两页对
同一本账给出相反读法，正是这轮campaign一直在找的那类错（边界 244 的邻居）。修法不是让历史页也沉默——断链本身
仍值得打印——而是让它用同一份结算读，并把"被裁定的"与"彼此吻合的"分开说：

```
links             every replacement link that no decision settled agrees with the other end it names
links             1 pair(s) were settled by a recorded decision, not by agreement: round-old.json -> round-new.json
 1  …  [settled with round-new.json by a recorded decision]  [refused by the queue]
```

`--campaign --json-out` 也带上了 `settlements`（一个脚本问"这一对是不是人定的"不必自己开文件），页面则打印
`1 pair(s) were settled by a recorded decision, not by agreement`，紧接着是第 111 轮的那行
`the last repair (…, settle-link) took 1 file(s) off the table: … -- cost 1 -> 0`——第 111 轮永远只能打印
"nothing left the table" 的那一格，到这里第一次有了内容。

**边界 251：矛盾没有能结算它的测量，所以裁定必须作为"裁定"存下来**——带时间、带理由，而不是把记录改成"看起来
本来就一致"。改记录会把一个人的判断变成原始证据。

**边界 252：结算终止收费，但不产生证据**——已结算的一对必须与"彼此校验后吻合的链接"分开打印，否则
"every link agrees" 会悄悄把一次人工决定算成一个证人。

**边界 253：顺序里的 `remedy` 必须是能清掉它的命令**——`--audit` 只是诊断；一个 plan 若把诊断排成第一动作，
它就在给没人做得到的工作排序。第 111 轮的记录之所以只能说"什么都没变"，是因为当时没有任何命令能改变它。

### 3.119 一条决定要有人、有理由：从"存下来了"到"能被复核"

第 112 轮把裁定写成一条记录，`--audit` 也印了一句 `A settlement says who decided`。跑完演示才发现这句话是空的：
`settlements.json` 里只有 `{record, successor, at, why}`，**根本没有"谁"这个字段**——页面在声称一个记录里不存在
的事实，正是这整轮 campaign 反复抓到的那一类错（边界 173 的邻居）。同时 `--settle-why` 是可选的，于是可以存下
一条没有任何解释的决定，而"没人能解释的决定"正是三个月后没人敢复核的那条。

第 113 轮把这两件事分开处理，因为它们的性质不同：

- **理由：新决定必填。** `settle_link` 在写入一条新记录前要求非空的 `why`，否则抛
  `a settlement needs a reason: --settle-why TEXT. A decision nobody can explain is the record nobody dares review`，
  CLI 以 2 退出且一个字节都不写。**已经存在的同一对**仍然幂等（返回 `changed: false` 与原时间），否则重复执行
  一条命令会开始索要它当初没要求的字段。
- **裁定者：默认空，但必须说成空。** `--settle-by NAME` 填 `by` 字段；不给就存空。这里刻意**不**用本机账户名填
  默认值——同一台机器谁在键盘前都可能，把 `DESKTOP\13423` 写成"决定者"是拿环境冒充人。

```
$ brian evolve --settle-link round-old.json=round-new.json \
      --settle-why "队列付的是 round-other.json；round-old 的声称已经过时" --settle-by alice
settled         round-old.json -> round-new.json
                recorded in var/evolve/settlements.json: this is a decision, not a measurement,
                and `--audit` prints it apart from the links that agree by checking.
                because: 队列付的是 round-other.json；round-old 的声称已经过时
                decided by: alice
repair plan      1 file(s) off the table: data/smd/stream-real.json -- cost 1 -> 0

$ brian evolve --settle-link round-other.json=round-new.json        # 新的一对，没给理由
cannot settle that link: a settlement needs a reason: --settle-why TEXT. A decision nobody can
explain is the record nobody dares review
（退出码 2，settlements.json 不变）
```

页面随之分成"有人"和"没人"两种读法：

```
$ brian evolve --audit
1 replacement link(s) were settled by a recorded decision, not by agreement:
  round-old.json             -> round-new.json              (2026-11-20T01:50:09.708367+00:00, by alice)

$ brian evolve --campaign
links             1 pair(s) were settled by a recorded decision, not by agreement: round-old.json -> round-new.json
links             decided by: alice 1
```

而第 112 轮留下的那条（有理由、没有裁定者）现在被如实说成空：

```
  round-old.json             -> round-new.json              (2026-11-20T01:37:35.544754+00:00, and no decider was recorded)
1 of them were recorded without --settle-by: the decision is dated but not attributed,
and a decision nobody is named for is the one nobody can be asked to defend.
links             decided by: and no decider recorded 1
```

`by` 缺失不是缺陷而是事实，所以它进自己的桶（边界 166），而不是被并进某个值里。第 113 轮还顺手加了一句：**理由**
缺失的旧记录（第 112 轮允许的形态）也会被点名 `N of them carry no reason at all: written before --settle-why was
required, and this build can only report them`——本构建没有"给已存决定补理由"的命令，而手工改 store 正是第 83/112
轮在防的事，所以这半件留给第 100 项。

**边界 254：句子声称的字段必须真的存在。** `A settlement says who decided` 在 `by` 出现之前是无据之言；一条记录
要能被复核，就得有人（或明确写"没人"）、有理由、有时间。

**边界 255：必填字段只对新记录必填。** 否则幂等重跑会开始索要它当初没要求的东西；一个命令的历史契约不能因为
新增字段而反过来追责旧的调用者。

**边界 256：默认值不能拿环境冒充人。** 本机账户是"谁在键盘前"，不是"谁做的决定"；把它填进 `by` 会让一条决定
看起来有归属，而实际上没有人可以被问。

### 3.120 补上的理由不是当时的理由

第 113 轮把"一条决定要有人、有理由"做成了校验：新的结算不给理由就写不进去。可校验只对未来生效——
`var/evolve/settlements.json` 里已经躺着第 112 轮写下的决定，它们有日期、有 successor，没有理由，也没有裁定者。
`--audit` 能做的只是报告它们（`carry no reason at all`）。最容易的修法是打开文件补一个 `why`，而第 83、112
两轮存在的理由就是：**手改账本会抹掉"谁决定的、什么时候决定的"这层信息**。所以第 114 轮加的是追加，不是改写。

```
$ brian evolve --settle-note round-old.json=round-new.json \
      --settle-why "the queue paid round-other, round-old kept the same successor" --settle-by alice
noted           round-old.json -> round-new.json
                the decision keeps its date (2026-11-19T20:00:00+00:00) and its successor; the explanation is appended
                because: the queue paid round-other, round-old kept the same successor
                explained by: alice
                `--audit` prints this apart from a reason the decision came with.
repair plan      nothing left the table: cost 0 -> 0, 0 file(s) still owing
```

存储形状是在原条目上追加，原有的三个字段一个都不动：

```json
{"record": "round-old.json", "successor": "round-new.json",
 "at": "2026-11-19T20:00:00+00:00", "why": "",
 "notes": [{"at": "2026-11-20T01:56:49.911843+00:00",
            "why": "the queue paid round-other, round-old kept the same successor",
            "by": "alice"}]}
```

`--audit` 因此要同时说两件事，而且不能合成一句：

```
1 of them carry no reason from the moment they were taken: written before --settle-why was required, and a
later note does not make the original decision explained
  round-old.json             -> round-new.json              explained later, on 2026-11-20T01:56:49.911843+00:00, by alice
1 of them were explained after the fact: the decision keeps its date, and the explanation came later.
```

`--campaign` 同理，把"事后补写"单独数一行：`1 of those decisions were explained after the fact: the record keeps
its date, and a later note does not make it explained at the time`。

这一轮真正的结论是**两处措辞随着机制一起改了**：第 113 轮那句 `carry no reason at all` 变成了
`carry no reason from the moment they were taken`，因为"它现在没有理由"已经不再为真了——它有了一条后补的说明。
`tests/test_settle_link.py` 里钉住旧句子的断言必须跟着改（边界 190）：机制变了而句子没变，页面就会在一个
能补写的账簿上继续宣称"nothing to do here"。

补写本身也是一次修复，所以它走 `record_repair(root, "settle-note", before, after)`，并如实打印
`nothing left the table: cost 0 -> 0`——说明改的是记录，不是计划里的欠账。这条"什么都没动"的输出不是噪音：
它是这次动作唯一被发现过的地方（边界 259）。

三种边界：

**边界 257：事后补上的说明不是当时的说明。** 一条决定的价值在于"它在当时被这样判断"，所以第 114 轮不覆盖
`why`，而是新增 `notes`；页面必须把两者分开打印，否则一次改写就能让旧决定看起来一开始就说清楚了——边界
174（修过的位置不是原来的位置）在"决定"这个对象上的版本。

**边界 258：只对新记录生效的校验，不能拿旧记录当非法。** 第 113 轮要求新结算必须有理由，但旧条目依然合法——
为它们提供的出路是追加说明，而不是让校验去拒绝一个已经发生的事实。对旧数据来说，正确的动作是补录，不是报错。

**边界 259：任何一次修改都必须至少在一处"自报"。** 补写说明既不动 `at`，也不动 `successor`，也不动计划，
所以它唯一能被读者看见的地方就是 `repair_history` 里那行"计划没有变化"。如果这次动作不记账，它就成了一次
看不见的写入——而看不见的写入正是第 83 轮之后所有工作的主题。


### 3.121 补写的说明也要有署名

第 114 轮让一条旧决定能补上说明，但只补了"是什么"，没补"是谁"。于是三种完全不同的情况在页面上长得一样：
一个人解释了三条决定、三个人各解释一条、一条说明没有署名。第 115 轮把这三件事拆开。

```
$ brian evolve --campaign
links             1 of those decisions were explained after the fact: the record keeps its date, and a later
                  note does not make it explained at the time
links             explained later by: alice 1
```

没有署名的那条不并进任何名字里，它自己一桶（`and no explainer named 1`）——边界 166（缺失字段必须自成一类）
在"说明者"这个字段上的第三次应用。

`--history` 的结算行原来只有"哪几对被裁定了"，读者要判断"这条决定解释过没有"得去翻 `settlements.json`。
现在同一行下面多一句：

```
links             1 pair(s) were settled by a recorded decision, not by agreement: round-old.json -> round-new.json
links             1 of them were explained later: round-old.json -> round-new.json (1 note(s), last by alice on 2026-11-20T01:56:49.911843+00:00)
```

两行而不是每行都标一遍（边界 241）：配对与条数属于这段历史，不属于某一条 round 行。

第三处是这轮最实际的一条。第 113/114 轮的规则合起来会产生一个死角：新的结算必须有理由，而**旧的决定**没有理由
——一个调用者拿旧的一条去重试 `--settle-link`，得到的是 `settled … was already recorded; nothing to change`
（看起来成功了），拿新的一条去试，得到的是"需要理由"。两种回答都没有说：真正缺理由的是那几个旧记录，而
`--settle-note` 就是修它们的命令。

```
$ brian evolve --settle-link round-other.json=round-new.json
cannot settle that link: a settlement needs a reason: --settle-why TEXT. A decision nobody can explain is the
record nobody dares review. If the decision is already recorded, `--settle-note RECORD=SUCCESSOR --settle-why
TEXT` adds the explanation without moving its date
hint            1 recorded decision(s) have no reason at all; `--settle-note RECORD=SUCCESSOR --settle-why TEXT`
                explains one without moving its date
exit: 2
```

`hint` 只在真的存在这种记录时才打印，并且给出条数：一句"你可以用另一个命令"没有数字，读者就没法判断值不值得跑。

三条边界：

**边界 260：说明的署名是说明的一部分。** 一个人解释三条决定与三个人各解释一条不是同一个事实，一条没署名的说明
也不是"某个人"的说明。三者必须分开数，否则"这些决定都解释过了"会把"没人愿意署名"读成"有人负责"。

**边界 261：拒绝必须指向能修它的命令。** `--settle-link` 拒绝一个没有理由的新决定时，如果不说旧记录可以用
`--settle-note` 补写，读者只知道自己这条不合规，不知道账本里那几条是可以修的——规则之间存在死角时，
报错文本就是唯一能指出另一条路的地方（边界 185 在"校验"上的版本）。

**边界 262：一页列出被裁定的决定，就要在同一页说清它有没有被解释。** "settled" 读起来像"已经决定并且已经说清"，
而实际可能只是"有人裁了，没人解释"。说明放在这一段历史的头上，而不是每一行，因为它是分组的属性（边界 241）。


### 3.122 一条决定的状态要落在它那一行上

第 115 轮把"有几条决定解释过"放进了页面：一行在结算段上面，一行在 `--history` 的段头。读起来顺，但有一个
具体的毛病——一个读者看到 `--history` 里某个 round 行，想知道"这条决定解释过没有"，仍然得回到段头去比对记录名。
状态是**行**的属性，被写在了**段**上。

第 116 轮把它移回行上：行尾的标记从 `[settled with X by a recorded decision]` 变成两种之一，

```
[settled with round-new.json and no reason was recorded]
[settled with round-new.json by a recorded decision]
```

后一种在有人补写过 note 之后出现（补写的说明算解释过，虽然它没跟着决定一起来——边界 257 管的是"什么时候写的"，
这里管的是"现在有没有")。段头那行"哪几对是被裁定的"保留：配对属于这段历史，状态属于行（边界 241 与 167 各自
管一半）。

第二件事是让口径只有一个。第 115 轮的解释者统计写在 `--campaign` 的打印里，`--json-out` 没有；这一轮把它抽成
`settlement_summary(settlements)`，四个状态各自计数：

```json
{"pairs": 1, "explained": 0, "explained_later": 1, "unexplained": 0,
 "deciders": {"": 1}, "explainers": {"alice": 2, "carol": 3}, "unattributed": 1}
```

`--audit --json-out["settlements"]` 与 `--campaign --json-out["settlement_summary"]` 都是这个函数的结果，页面上
那几行也用它——脚本和读者看到的不是"两次恰好一致的独立计算"，而是同一份账（边界 244）。

第三件事是最小但最像账本的一条：存储按时间排序，所以同一作者在**同一秒**写下两条不同理由时，页面无法说清
哪条在先。真实命令很难撞上（时间戳到微秒），但回放脚本会给显式的 `now`：

```
first note written: 5
refused: two different explanations for round-old.json -> round-new.json carry the same second
         (2026-11-20T02:00:00) from the same author; a decision read in order needs them to be distinct in time
notes after the refusal: 5
```

不同作者在同一秒各写一条是允许的——那是两个人在解释同一件事，不是一个人在自相矛盾。这条规则的第一版按微秒
比较，结果把这种正常情况也拒了；真正的对象是"作者 × 秒"，边界 263 就是它。

三条边界：

**边界 263：排不出先后的东西不许当成序列。** 存储的时间精度决定了它能排序的最小单位；同一作者在同一精度单位内
给出两条不同说明时，正确动作是拒绝并说明理由，而不是按写入顺序列出来假装那是过程。

**边界 264：一页的四个状态必须来自一次计算。** 只要页面与 JSON 各自统计，它们迟早会在某个边界条件上分开
（第 82 轮的"闸门收费的事实必须出现在页面上"是同一个问题的上一版）。统计抽成一个函数，页面和载荷都读它。

**边界 265：状态属于它描述的那一行。** 一个读者在列表里找"这条解释过没有"，不会先读段头再回来比对；把状态
放在段上等于要求读者做一次手工连接（边界 167 说事实属于它描述的对象，这一条说：对象是行，就不要放在组上）。


### 3.123 一条没人能解释的决定，是一项可以收费的债

第 116 轮把四个状态算清了：带着理由来的、后来补上说明的、两者都没有的、以及四种里各自"有名字/没名字"。但 `unexplained` 只是页面上的一个数字：`--strict` 永远为它返回 0，于是"有 3 条决定没人能复核"这句话说完就完了。第 154 条边界（能付的欠账才进闸门）说的是"要有命令能还"，而这里恰好有一条：`--settle-note`。既然能还，就该收。

**收的是"过期的"，不是"存在的"。** 默认不收费：`--max-unexplained-days N` 关着时闸门不为它退 1。原因和第 255 条边界同源——`settlements.json` 里躺着 `--settle-why` 强制之前写的记录，一个默认让整本账永远红灯，红灯久了就没人看。页面无论有没有 cap 都把年龄印出来，cap 只决定"什么时候变成退出码"。

```
$ brian evolve --history
decisions       1 settled decision(s) have no explanation: no reason was recorded with them, and no note was added since
  round-old.json             -> round-new.json              (decided 2026-11-10T02:20:46.784974+00:00, 10.0 day(s) ago)
                `brian evolve --settle-note RECORD=SUCCESSOR --settle-why TEXT --settle-by NAME` explains one

$ brian evolve --audit --strict --max-unexplained-days 3     # exit 1
1 decision(s) have had no explanation for longer than the 3 day(s) asked for:
  a decision with no reason and no note is debt nobody can review, and `--settle-note RECORD=SUCCESSOR`
  with `--settle-why TEXT --settle-by NAME` is the command that pays it: --strict exits 1 (boundary 266).

$ brian evolve --audit --strict                               # exit 0：没有 cap 就没有收费
$ brian evolve --audit --max-unexplained-days 3               # exit 2：flag 不带 --strict 会被静默忽略
--max-unexplained-days caps a --strict charge; without --strict nothing reads it
```

**还掉的样子。** 缴费不是"编辑 JSON"，而是一条被记录的说明：

```
$ brian evolve --settle-note round-old.json=round-new.json \
      --settle-why "two books named the same successor; the retirement was right and the record keeps saying so" \
      --settle-by alice
noted           round-old.json -> round-new.json
                the decision keeps its date (2026-11-10T02:20:46.784974+00:00) and its successor; the explanation is appended
                because: two books named the same successor; the retirement was right and the record keeps saying so
                explained by: alice
                `--audit` prints this apart from a reason the decision came with.
repair plan      nothing left the table: cost 0 -> 0, 0 file(s) still owing

$ brian evolve --audit --strict --max-unexplained-days 3     # exit 0，`decisions` 那一行消失
```

注意 `repair plan nothing left the table`：补一条说明**不会**让书账少一件——它付的是"没人能复核"这笔债，不是"两本书互相矛盾"那笔（第 111 轮的发现）。两条债各有各的命令。

**每个域只欠自己那几条。** `--campaign` 的每一行现在带 `settlements`：

```
$ brian evolve --campaign --json-out c117.json
[('SMD', {'unexplained': 1})]
{'pairs': 1, 'explained': 0, 'explained_later': 0, 'unexplained': 1, 'deciders': {'': 1}, 'explainers': {}, 'unattributed': 1}
```

整本账的 `settlement_summary` 和每一行的 `settlements` 是同一份计算的两个投影（`_round_source` 用裁定所指轮的 `stream.source` 定归属，退役的轮也算——裁定可以针对一个后来被取代的轮）。页面每个数字都按域分，新数字不按域分，就是第 69 轮那个缺陷换个字段。

**三条边界**

**边界 266：循环永远填不上的债，必须由闸门收、由一条命令还。** "没人解释的决定"不是"待处理"，因为没有任何测量会补上它，只有人会。既然有一条命令能还（`--settle-note`），一个永远返回 0 的闸门就等于把这笔债留给仔细的读者。

**边界 267：规则不能追溯收费。** cap 默认关、页面始终印年龄、旧记录只报告不收费（255/258 的同一条规则，用在一笔钱上）。一个让历史永远违约的默认值，最终会让整个检查被绕过。

**边界 268：按域印的页面，新数字也要按域印。** 一本账级的"1 条决定没人解释"，在每行都写着域名的页面上等于没说清楚是谁欠的；而"谁欠"正是决定"该去修哪本书"的那一半信息。

### 3.124 一份裁定说的是「书当时长这样」：书动了，裁定就得重来

第 112 轮把一次结算存成一条决定：`round-old.json` 对 `round-new.json` 的主张在这一刻被放弃。第 113–117 轮给这条决定补了理由、署名、补写说明和「没人能解释」的收费。它们都默认一件事：决定一旦写下，它所指的那对账簿就一直长那样。

**本轮要修的是这个默认。** 决定的对象是**账簿当时的状态**，而账簿会动：

1. 被裁定的记录可能被重新测量，改成指向另一个后继（`it now names X as its successor`）；
2. 它可能连退休标记都没了（`it carries no retirement mark any more`）；
3. 它可能整个从账簿里消失（`the settled record is no longer in the ledger`）；
4. **同一后继**上可能出现一条决定之后才到达的主张（`N claim(s) on the same successor arrived after the decision`）。

前三条都是「决定描述的那对东西不在了」。第四条不一样：决定当时只覆盖了它看到的那些主张，一条后来的主张是决定**从未见过**的新事实，而页面如果继续把它印成「已结算」，就等于替那条新主张作了保。

#### 规则：只认「有日期的、晚于决定的」主张

第一版规则把**任何**另一个主张者都算作证据动了。跑测试时 11 条里 7 条立刻挂掉——因为第 112 轮的设计是**只消掉一个主张者的主张**，另一个端点是刻意留着给人看的（边界 252）。照第一版规则，每一次对 `mismatch` 的裁定当场就是陈旧的，这条检测等于永远报警，而永远报警的检测不会被读。

第二版规则要回答的是「这条主张是**在决定之后**才出现的吗」，于是需要给主张找日期。第一次用的是「退休标记的 `at`，没有就用这一轮的 `finished_at`」，结果在演示里又把两条本来摆在面前的旧主张算成了「刚刚到达」——`finished_at` 是那一轮**跑完**的时间，而退休标记是事后（第 83/93 轮）才写下的，两者可以差好几天。**主张算不算「后来」，只能看它自己的 `at`；没有 `at` 的主张既不能证明在先、也不能证明在后，于是它既不移动证据、也不被算成移动（边界 272）。**

```
1  2026-11-18T06:05:00+00:00  ...  [settled with round-new.json by a recorded decision]  [refused by the queue]
audit:
1 replacement link(s) disagree, so the two ends of the relation cannot both be true:
1 replacement link(s) were settled by a recorded decision, not by agreement:
1 of them rest on books that have moved since the decision was taken:
  round-old.json             -> round-new.json              (1 claim(s) on the same successor arrived after the decision: round-third.json)
A decision is a statement about the books as they stood; when they move, the decision must be taken again (boundary 269).
```

第二个演示里被裁定的记录干脆不再指认那个后继：

```
1 of them rest on books that have moved since the decision was taken:
  round-old.json             -> round-new.json              (it carries no retirement mark any more)
```

`--history` 印的是同一句话的另一种主语（「自那次决定以来，账簿已经动过」），`--campaign` 把它按流分开计：

```
settled         1 decision(s) rest on books that have moved since they were taken:
                a decision is a statement about the books as they stood; when they move, take it again
campaign json:  {"settlements_moved": [{"record": "round-old.json", "successor": "round-new.json",
                 "reasons": ["1 claim(s) on the same successor arrived after the decision: round-third.json"],
                 "claimants": ["round-third.json"]}], "domains": [{"domain": "SMD", "settlements": {"moved": 1}}]}
```

#### 为什么这一轮一分钱都不收

`--audit --strict` 对「证据动了」仍然是 0。这不是忘了收，而是本版本里**没有任何命令能重开一条决定**：`--settle-link` 对同一对会幂等地返回「已经记过」，`--settle-note` 只加说明。一个付不掉的债收进闸门，只会让 `--strict` 永远红着、然后被关掉（边界 154/171/266 的同一条规则）。第 105 项才是那条命令：一次「重新确认」把决定重新按今天的账簿写一遍，于是「说明有了、证据却动了」才变成可付、可收的状态。

#### 这一轮读出来的三件事

1. **决定是关于时刻的陈述**，不是关于名字的。页面按名字把 `round-old.json -> round-new.json` 印成「已结算」时，它其实在说一句过去时的话，而读者会把它当成现在时（边界 269）。
2. **记录自己也会动**：被重测、被改指、被删除，决定描述的那一对可能已经不在了。只按名字匹配的检测会把它当成成立（边界 270）。
3. **收不了的债只能报告**：一处缺陷要先有能清掉它的命令，才配进闸门（边界 271）。这条规则在本仓库已经被独立发现过三次（154/171/266），这是第四次。


### 3.125 决定可以重开：把「书动了」变成一次重新确认

第 118 轮让页面说出「这条决定所依据的账簿，在决定之后已经动过」，然后什么也不做——因为本版本里没有任何命令能重开一条决定，闸门于是永远不能对这件事收费（边界 271 的第四次出现）。本轮补上那条命令，并且把「什么时候算动过」变成一个可以推进的时刻。

`_effective_moment(item)`：一条决定**现在**说话的时刻，是它的 `at` 和它所有 `reaffirmations[].at` 里最新的那个。这不是装饰——重新确认就是操作者说「我读过今天的账簿了，这条决定仍然成立」；如果不推进这个时刻，第 118 轮的规则会把同一批主张永远算成「后来才到」，重开一次也毫无变化。

`--settle-reaffirm RECORD=SUCCESSOR --settle-why TEXT --settle-by NAME` 调用 `reaffirm_link(...)`：保留第一次的日期（问题是什么时候被裁定的），追加一条 `{at, why, by, claimants}`。四条拒绝，各有各的理由：

- 理由为空：`a re-affirmation needs a reason: --settle-why TEXT. Re-taking a decision without saying what changed is how a decision nothing supports stays alive`
- 记录不在：`the decision describes a record the ledger no longer holds, so there is nothing to re-affirm`
- 记录已改指：`a re-affirmation would assert something the ledger denies. Take a new decision with --settle-link, or drop this one`
- 这条决定根本没记过：`--settle-link` records a decision, `--settle-reaffirm` re-takes one`

**它必须写下自己覆盖了什么**（边界 274）：`what it covered: round-other.json, round-third.json`。读者据此区分「对着三份主张重新读过」和「随手点了一下」。

收费默认关闭，只针对**可清掉**的那一类：`--max-moved N` + `--strict`，而 `moved_clearable` 只留「pair 还在、只是有主张后来才到」的行。记录已经不在、退休标记被撤掉、或已经改指别处的，仍然只报告不收费——那不是重开能解决的，得重新做一条决定（边界 275）。

演示（`var/demo119`，日期挪到真实过去，因为 CLI 的重新确认发生在**现在**）：

```
$ brian evolve --audit --strict --max-moved 0
1 of them rest on books that have moved since the decision was taken:
  round-old.json             -> round-new.json              (1 claim(s) on the same successor arrived after the decision: round-third.json)
1 decision(s) rest on books that have moved since they were taken, above the 0 asked for:
  ... `--settle-reaffirm RECORD=SUCCESSOR --settle-why TEXT --settle-by NAME`
  re-takes it against today's books: --strict exits 1 (boundary 273).
   exit 1

$ brian evolve --settle-reaffirm round-old.json=round-new.json --settle-why "read today's books" --settle-by alice
reaffirmed     round-old.json -> round-new.json
                the decision keeps its first date; this re-affirmation is on 2026-11-20T02:49:36.232874+00:00
                what it covered: round-other.json, round-third.json
                re-affirmed by: alice
   exit 0

$ brian evolve --audit --strict --max-moved 0      # 同一条收费，现在无事可收
   exit 0
```

再让一份主张在重新确认之后一小时到达，`--history` 就把两种状态并排摆出来：

```
settled         2 decision(s) rest on books that have moved since they were taken:
  round-old.json             -> round-new.json   (...)  [re-taken 1 time(s), last on 2026-11-20T02:49:36.232874+00:00]
  round-third.json           -> round-new.json   (...)  [never re-taken: only --settle-reaffirm moves the date it speaks about]
```

两个发现。第一个是自己的：`clearable` 一开始写成「理由里没有 successor / ledger 就算可清掉」，而「后来的主张」那句本身就含 `successor`——于是每一行都成了不可清掉，本轮唯一想收费的那一类恰好被自己的子串判断排除掉；自测第一轮就挂，改成布尔 `pair_gone`。第二个是文档的：第 118 轮的插入脚本从 `\n104. ` 重写到文件末尾并「追加第 105 项」，而第 105 项从未出现在文件里——§6 的尾部从 104 直接结束。只往末尾追加的队列，末尾就是它唯一的锚点，丢了没人会撞上；本轮把 105（本轮）与 106（下一步）补上，边界 274 的另一面：追加本身也要能被检查。

**边界 273：一条决定的日期，是它最后一次被确认的时刻，不是它第一次被做出的时刻。** 重开一条决定必须推进它说话的那个时刻，否则同一批主张永远算「后来才到」，重开等于没开。

**边界 274：一次重新确认必须记下它覆盖了哪些主张。** 否则读者分不清「对着三份主张重新读过」和「随手点了一下」，而这正是这条记录存在的理由。

**边界 275：清不掉的债继续报告，只对能清掉的那一类收费。** 这是 154/171/266/271 的第四次出现：`retargeted`/`gone` 的 pair 没有东西可重新确认，闸门对它收费只会训练人忽略闸门。

### 3.126 一笔债要有一个文件，一个动作要能带多笔

第 119 轮让「书动了」这件事有了命令，但命令一次只能带一对，而且这一笔债在页面上没有名字：`settlement_moved` 从第 118 轮起就只出现在 JSON 里，读者想看「几条决定被重开过、它们属于哪个文件」必须自己去翻账本。于是「先打开哪个文件」这个问题——它恰好是「书动了」之后必然出现的下一个问题——在页面上没有答案。同时 `--settle-reaffirm` 一次一对，意味着一次走查要把同一条理由手打十一遍，而重复手打的理由就是会漂移的理由。

这一轮做两件事：把债按来源文件排成一张表，并让一个动作带上多对。

**一张表：`reaffirm_plan`**

`moved_settlements` 的每一行现在都带 `origin`（记录自己的 `stream_path`），`reaffirm_plan(rounds, settlements)` 按这个来源分组：

```python
{"origin": ..., "decisions": n, "stale": k, "retaken": r, "records": [...],
 "commands": ["brian evolve --settle-reaffirm <record>=<successor>", ...]}
```

只保留 `stale` 非零的文件，按 `(-stale, origin)` 排序，命令照第 101 轮的规矩写成一条能直接粘进终端的字符串——页面印出来的清单，必须是命令能跑的那份清单。`--campaign` 因此多两行：

```
links             2 decision(s) rest on books that have moved since they were taken: round-other.json -> round-new.json, round-fifth.json -> round-new.json
links             re-take next: data/smd/stream-two.json -- 2 stale of 2 recorded decision(s) -- brian evolve --settle-reaffirm round-other.json=round-new.json
```

第一行是第 118 轮算出来、到这一轮才写到页面上的事实；第二行是它的下一步。JSON 里同时有 `settlements_moved` 与 `reaffirm_plan`，脚本不用解析页面。

**一个动作：`--settle-reaffirm` 带多对**

`nargs="+"`，一条理由，一条 `--settle-by`，若干对：

```
$ brian evolve --settle-reaffirm round-old.json=round-new.json round-other.json=round-new.json \
    --settle-why "read today's books" --settle-by alice
reaffirmed     round-old.json -> round-new.json
                the decision keeps its first date; this re-affirmation is on 2026-11-20T03:06:58.362463+00:00
                nothing had moved since 2026-11-20T02:49:55.734918+00:00: this records a fresh confirmation, not a re-take
reaffirmed     round-other.json -> round-new.json
                what it covered: round-fifth.json, round-fourth.json, round-third.json
re-taken       2 of 2 decision(s), 0 kept its date (already re-taken), 0 refused
```

一条被拒绝的 pair 不会中止其余的（第 277 条边界）：拒绝的原因写一行，`refused` 加一，循环继续。命令以退出码 2 收尾表示有人被拒——批次不是事务。

**第三个发现：`nothing had moved since` 与「已经重开过」不是同一句话**

第 119 轮的幂等分支按**微秒**判断「同一时刻」，所以它对人是不可达的：两次调用之间的时间差总是存在。真正能观察到的事实是「上一次确认之后，书有没有动过」：

- 动过：这次是**重开**（retakes），理由必须写；
- 没动：这次是**一次新的确认**（fresh confirmation），它记录的是「我今天读过账本，书没变」，同样有价值，但不是重开。

两种情况都追加到 `reaffirmations`——一条决定被确认两次是两件事，页面上要说清是哪一件（第 276 条边界）。CLI 因此在调用 `reaffirm_link` **之前**先算一遍 `moved_settlements`：调用之后再算永远得到「没动」，因为这次调用本身就把那些 claim 覆盖了。这个顺序错误在自己的演示里出现过一次，写法上留了注释。

**第三个发现：页面上的清单不能有重名**

第一次演示时 `what it covered` 印出了 `round-old.json, round-old.json`：`history()` 会把同一轮从 round 文件与 ledger 各读一次（第 34 轮的合并规则），`sorted(...)` 于是把同一个名字排了两遍。改成一个集合，并且用 `sorted({...})` 保序去重。候选清单重复一次，读者会数出两条相反的证据。

**第四个发现：演示夹具的日期必须落在过去**

`var/demo119.py` 用 `NOW - timedelta(days=5)` 造日期，而 `NOW` 是 2026-12-01——比机器时钟晚 11 天，于是「后来的 claim」其实还在未来，任何重开都覆盖不了它，演示会显示一笔没有命令能还的债。`var/demo120.py` 复制夹具后把每个晚于当前时刻的 `retired.at` / `settlements.at` 拉回 `now - 6 days`。这条与第 119 轮的第 (2) 个夹具缺陷是同一个病：`NOW` 是给测试用的固定时刻，不是「今天」。

**边界**

- **边界 276**：一条决定被确认两次是两件事，页面必须说清是哪一件——「重开」与「书没动，只是再确认一次」含义不同，混成一句会把「有人复核过」读成「账本变过」。
- **边界 277**：一批动作在第一次被拒时就整体停下，会教操作者手工重跑剩下的部分，而手工重跑正是理由漂移的入口；拒绝要记，循环要继续，退出码要说话。
- **边界 278**：`--json-out` 的形状不能取决于参数个数——`{"reaffirmed": {...}}` 与 `{"reaffirmed": [{...}, {...}]}` 是两种类型，读它的人要写两个分支。现在永远是列表，外加 `reaffirm_refused` 计数。

**这一轮没有做的事**

`reaffirm_plan` 只按文件分组，没有按时间排序；「哪一天重开、隔了几次走查」留给第 107 项。`--max-moved` 仍然是按条数设上限，按文件设上限也留给第 107 项。
### 3.127 同一笔债，按文件算和按条数算不是一件事

第 120 轮让 `--campaign` 把「哪些文件上的裁定过期了」列出来，并给了每个文件一条可以直接粘贴的命令。这一轮补的是同一句话的另一半：**什么时候**看过，以及从现在起又走了几趟。

- `reaffirm_plan` 的每一行多了 `last_retaken`（最近一次重新确认的时刻；从没重开过就是空串）和 `walks_since`（在那之后记录的走查次数）。`_walks_since(runs, moment)` 在 `runs is None` 时返回 `None`，页面把它印成 `unknown` —— 「没人问过」和「从那以后确实没走过」是两条不同的事实（边界 280）。
- `--campaign` 的 `re-take next` 行因此读作：

```
links             re-take next: data/smd/stream-two.json -- 1 stale of 2 recorded decision(s) (1 re-taken), last re-taken 2026-11-20T03:21:35.406202+00:00, 0 walk(s) since -- brian evolve --settle-reaffirm round-fifth.json=round-new.json
```

- 新增 `--max-moved-per-file N`：和 `--max-moved N` 并列，但计的单位是一个文件。同一个账本上，`--max-moved-per-file 0` 退出 1 并印出：

```
1 decision(s) on one stream file rest on books that have moved, above the 0 asked for for a single file:
  the cap is per file because the work is per file: eleven stale decisions on eleven files is eleven
  afternoons, and eleven on one file is one. Re-take that file's decisions with
  `brian evolve --settle-reaffirm RECORD=SUCCESSOR --settle-why TEXT`, which `brian evolve
  --campaign` lists one command per pair (boundary 279).
```

  而 `--max-moved 5` 在同一份账本上退出 0：总数远远没到上限，但那一个文件的账确实没人读过。两个上限回答的是两个问题，谁也不能替谁。
- `--max-moved-per-file` 和别的上限一样，不带 `--strict` 时直接退出 2（`--max-moved-per-file caps a --strict charge; without --strict nothing reads it`）：一个闸门从不读的旗子看起来像是上膛了。
- `--campaign --json-out` 的 `reaffirm_plan` 行现在带全时间线：

```
[{'origin': 'data/smd/stream-two.json', 'decisions': 2, 'stale': 1, 'retaken': 1,
  'records': ['round-fifth.json'],
  'commands': ['brian evolve --settle-reaffirm round-fifth.json=round-new.json'],
  'last_retaken': '2026-11-20T03:21:35.406202+00:00', 'walks_since': 0}]
```

三条读数：

1. **上限的单位就是读者付钱的单位**。十一笔过期裁定散在十一个文件上，是十一个下午；十一笔挤在一个文件上，是一个下午。总数上限把两者读成同一个数字，而这个人要打开的是文件，不是总数（边界 279）。
2. **「过期」没有日期就是半句话**。`last_retaken` 让「这个文件上有 1 笔过期」变成「今天早上重开过、之后又走了三趟」或「一个月没人碰过」——同一个计数，两种处置。
3. **没人算过的计数要印成 unknown，不能印成 0**。`walks_since = 0` 是一条测量：账本里确实没有更晚的走查；`None` 是「调用方没给走查记录」。把后者印成 0，等于让页面对一个它没看过的文件作证（边界 280）。

**边界 279：上限要写在读者付钱的那个单位上。** 十一笔散在十一个文件上是十一个下午，十一笔在一个文件上是一个下午；按总条数设上限，会把这两种欠账算成同一笔，而处置它们的人手只有一双。

**边界 280：没人算过的计数必须印成 unknown，不是 0。** `0` 是一条测量（确实没有更晚的走查），缺失是一次没问；把没问的印成 0，是让页面对没看过的东西作证。

**边界 281：页面上并列的两个上限，要在同一行里各自说出自己的问题和答案。** `--max-moved` 与 `--max-moved-per-file` 相邻而治不同的欠账；只印其中一个的判定，读者会以为另一个也过了。


### 3.128 同一份欠账有两种活：从没读过 vs 读过又变了

第 121 轮给计划加了时间线，也给了「按文件收费」的上限，于是页面上出现了这样一行：

```
links             re-take next: data/smd/stream-one.json -- 3 stale of 3 recorded decision(s) (1 re-taken), last re-taken 2026-11-16T03:32:40.173907+00:00, 1 walk(s) since, 2 never re-taken, 1 re-taken before and moved again -- brian evolve --settle-reaffirm-file data/smd/stream-one.json --settle-why TEXT --settle-by NAME
```

`3 stale of 3` 是一个数，但它是两件事的和：

- **从没读过**（`never re-taken`）：裁定时书是这样，之后书动了，而这条决定一次都没被重新确认过；
- **读过又变了**（`re-taken before and moved again`）：确认过一次，之后又有新的主张落在同一个后继上。

这两件事的工作量完全不同。第一种是第一次读——需要把整份记录读一遍；第二种是复查——只需要对比上次确认之后的变化。混在一个数字里，读者无法估计要花多少时间，也无法判断「刚刚重开过，为什么又欠着」。

所以桶里分了两栏（`bucket["never"]` / `bucket["again"]`），行里还保留了 `pairs`（`[{record, successor}]`，按名字排序），因为「一条命令付一整个文件」需要一组对子，而不是一个计数。

### 一条命令付一整个文件

第 120 轮让 `--settle-reaffirm` 收多个对子，但清单还是要人手抄。第 122 轮把它变成一条命令：

```
$ brian evolve --settle-reaffirm-file data/smd/stream-one.json --settle-why "read today's books" --settle-by alice
re-taking      3 decision(s) on data/smd/stream-one.json in one command (2 never re-taken, 1 re-taken before and moved again)
reaffirmed     round-fourth.json -> round-new.json
reaffirmed     round-old.json -> round-new.json
reaffirmed     round-third.json -> round-new.json
re-taken       3 of 3 decision(s), 0 kept its date (already re-taken), 0 refused
repair plan      nothing left the table: cost 0 -> 0, 0 file(s) still owing
```

关键实现决定：这个分支**不自己实现一遍**，它把 `pairs` 塞进 `args.settle_reaffirm` 再递归调用第 120 轮的分支。于是「一条理由用于所有对子」「每个对子一块输出」「被拒绝的对子不打断其余」「`--json-out` 是列表」这些规则各只有一份实现。第二份实现就是第二套规则。

对子来自**页面上印的那个计划**（`reaffirm_plan`），不是重新数一遍：`match = [row for row in plan_rows if same_stream_path(...)]`。否则页面和命令会各自用不同的过滤条件，抄下来的清单和跑出来的清单就会漂移。

付完一个文件，页面的下一行就换到另一个文件——这正是「按文件收费」的意义：

```
$ brian evolve --audit --strict --max-moved-per-file 0
2 decision(s) on one stream file rest on books that have moved, above the 0 asked for for a single file:
...
$ brian evolve --settle-reaffirm-file data/smd/stream-two.json --settle-why "read today's books" --settle-by alice
re-taking      2 decision(s) on data/smd/stream-two.json in one command (2 never re-taken, 0 re-taken before and moved again)
re-taken       2 of 2 decision(s), 0 kept its date (already re-taken), 0 refused
$ brian evolve --audit --strict --max-moved-per-file 0
$ echo $?
0
```

**边界 282：一个文件的 stale 数藏着两种不同的活。** 第一次读和「读完之后又变了」的复查，成本不同、产物不同、下一页该说什么也不同；把它们压在同一个数字里，重新确认就看起来像例行保养。

**边界 283：批量的对子必须来自页面印的那个计划。** 若命令自己重新筛一遍，抄下来的清单和跑出来的清单就会漂移，而漂移的方向总是「多跑了没人看的」或者「漏了刚刚变脏的」。

**边界 284：复用单条分支是正确做法，复制一份实现不是。** 拒绝规则、理由规则、输出形状、退出码都只该有一份；第二份实现不会报错，它只会慢慢和第一份不一致。

### 3.129 两种欠账要能分开付：`--settle-kind`，以及域名行漏掉的那一栏

第 122 轮把「一个文件上的 stale 数」拆成了 `never re-taken` 与 `re-taken before and moved again`，但命令仍然是一次把整个文件都重开。于是出现一个很奇怪的动作：只欠「复查」的人必须把「第一次读」也一起读了，而一条理由只能写一次——

```
$ brian evolve --settle-reaffirm-file data/smd/stream-one.json --settle-kind never --settle-why "first readings" --settle-by alice
re-taking      2 decision(s) on data/smd/stream-one.json in one command (2 never re-taken, 1 re-taken before and moved again)
leaving        1 decision(s) of the other kind alone: `--settle-kind never` was asked for, and the two kinds are different work (boundary 285)
reaffirmed     round-fourth.json -> round-new.json
reaffirmed     round-third.json -> round-new.json
re-taken       2 of 2 decision(s), 0 kept its date (already re-taken), 0 refused
```

`--settle-kind never|again|all`（默认 `all`）只重开一种。留下的那条不会被顺带标成「读过」——付便宜的那一半，不会把贵的那一半也盖章：

```
$ brian evolve --settle-reaffirm-file data/smd/stream-one.json --settle-kind again --settle-why "the books moved again" --settle-by alice
re-taking      1 decision(s) on data/smd/stream-one.json in one command (0 never re-taken, 1 re-taken before and moved again)
reaffirmed     round-old.json -> round-new.json
```

载荷也说了它留下了什么：`--json-out` 里除了 `reaffirmed` 与 `reaffirm_refused`，还有 `reaffirm_skipped`。一个批量命令只说「我做了 N 条」而不说「我跳过了 M 条」时，跳到下一条的命令和同一条命令跑两遍看起来是一样的。

### 域名行也要有这两栏

第 122 轮把两栏加到了**文件**行上，域名行仍然只有一个 `moved`。于是一个欠三条「第一次读」的域和一个欠三条「复查」的域，在 `--campaign` 的行里长得一模一样。这一轮把两栏按「决定所属轮次的流」归到域名行：

```
$ brian evolve --campaign --json-out
domains[0].settlements == {"moved": 3, "stale_never": 2, "stale_again": 1}
```

### 一个被测试逼出来的修正

第一版把 `reaffirm_plan` 里**所有** pairs 都按 kind 记进域名行。第 118 轮的一个测试随即失败：一条记录被删掉了退场标记，它确实 stale，但没有任何命令能付它——`--settle-reaffirm` 会拒绝（重开等于断言账本否认的事）。把它记成 `stale_never` 之后，域名行的两个数加起来比任何命令能做的活都多。

所以分类跟着**可付集合**走（`moved_clearable`，也正是门禁收费的那个集合）：不可付的那条只记在 `moved` 里，不进任何 kind。

**边界 285：两种活不能共用一个理由。** 第一次读要把整份记录读一遍，复查只需要对比上次确认之后的变化；一次命令把两种都做掉，贵的活就隐形了，而且一条理由同时替两件事作证。

**边界 286：按文件分的栏，也要按域分。** 一个域欠三条「第一次读」和一个域欠三条「复查」，在只印总数的行里是同一个页面；而它们该看到的下一页不同。

**边界 287：数的东西必须是命令能付的东西。** 把无人可付的条目并进某一类工作里，读者会按那个数去排期，而排出来的活干不完——分类跟着可付集合走，跟着计划列表走就会多算。

### 3.130 反复重开说明的是决定，不是账本

`settlements.json` 一直存着两样东西：决定被作出的时刻 `at`，以及每一次重开的 `reaffirmations`（带时刻、理由、署名和「这次覆盖了哪几条主张」）。可页面上能说的只有「重开过」，说不出「重开过几次、按什么顺序、每次覆盖了什么」——于是一个被重开三次的决定和一个只重开过一次的决定，看起来是同一行字。

这一轮把时间线做成一等事实：

```
$ brian evolve --campaign
links             1 decision(s) have been re-taken at least 3 time(s), which usually means the decision is wrong and not that the books moved: round-old.json -> round-new.json (3 time(s), last 2026-11-17T03:49:46.164802+00:00)
```

```
$ brian evolve --history
 1  ...  [retired, re-measured as round-new.json]  [settled with round-new.json by a recorded decision]  [re-taken 3 time(s): the decision keeps going stale]
```

载荷里是一份有序的时间线，第一条是决定本身（`kind: taken`），后面每条是重开（`kind: retaken`，带署名与覆盖面）：

```json
["taken:", "retaken:alice", "retaken:bob", "retaken:alice"]
```

第一版的 `by` 是空字符串——决定是 `_settled_without_reason` 造的，本来就没有署名。空着是对的：那是一份没人署名的决定，而第 113 轮的规则是「默认值不能让环境冒充人」。

### 为什么阈值是三次

一次重开的意思是「我读了今天的账本，这条决定还成立」。账本动一次是常事，所以一次、两次重开都还算「书动了」。第三次之后，最省力的解释换成了另一个：这条决定本身选错了——它认的那个后继不断有人来主张，或者它本来就该被换成一条新决定。

所以页面的措辞是 `which usually means the decision is wrong and not that the books moved`，而且**点名**：点名的目的不是让读者再重开一次，而是让读者去看那条主张本身。

### 一个被演示逼出来的修正

第一版把 `[re-taken N time(s)]` 标在所有匹配 `own_name in pair` 的行上，于是**后继记录**那一行也写着重开次数——而后继从来没有被「重开」过，它是被裁定的那一边。演示里那两行读起来像「某个从没做过决定的轮次被重开了六次」。

修法是这条标注只落在决定settle的那条记录自己的行上（`own_name == pair[0]`）。这和第 107 / 241 轮同一条规矩：一条事实标到它并不描述的行上，读者就分不清它说的是哪条记录。

**边界 288：反复重开是「关于决定」的证据，不是「关于账本」的证据。** 第一次重开说明账本动了；第三次之后更省力的解释是决定选错了，页面该点名让读者去看那条决定，而不是再排一次重开。

**边界 289：时间线要能读出顺序，并且区分「作出」与「重开」。** 同一对记录的两类条目含义不同（一个是原始决定，一个是复核），混成一条列表读者就无法判断哪条是第一次。

**边界 290：只有次数而没有时刻，读者无法拿它跟账本核对。** 时间线是原始事实，次数是从它派生的；先有可核对的条目，才有可引用的数字。
### 3.131 撤销一条决定，不能顺手把一个矛盾藏起来

一份结算说的是「这两份记录争同一个后继，我把其中一条按下去」。按下去了，`replacement_links` 就不再报这条矛盾，
`--verify` 也不再拒它——这是第 251 条边界要的效果。但它同时也说明：**这条决定可能本来就是错的**。第 124 轮把
「反复重开」挑了出来，说那多半是决定的问题；挑出来之后没有任何命令能处理它：`--settle-reaffirm` 是「再读一遍，
决定仍然成立」，`--settle-note` 是补理由，两个都假设决定继续站着。于是这个循环里最重要的一句话——「这条决定不算了」
——只能靠手改 `settlements.json`，而手改不留任何人、任何时间。

第 125 轮加的是一条命令和一个字段。

```
brian evolve --settle-drop RECORD=SUCCESSOR --settle-why TEXT [--settle-by NAME]
```

它把 `{at, why, by}` 追加进这条决定自己的 `withdrawn` 列表，决定的 `at`、`successor`、`why` 一个字都不动。保留是
关键：被撤销的决定仍然是「某人在某时做过的决定」，历史里少一条和记录一条「它后来不算了」不是同一件事。

然后，所有回答「这一对算不算解决了」的地方，都改成只读**还站着的**决定（`active_settlements`）：

```
active_settlements(settlements)  ->  is_withdrawn(item) 为假的决定
replacement_links(rounds, settled)  读 active_settlements
provenance_block / blocked_pairs    读 active_settlements
summary / dossier / plan            数量只数还站着的
```

真实的账本上，`var/demo125`（第 124 轮那份三条重开的账，剥掉上次的撤销、把 ledger 里重复的两行收掉、日期全压到过去）：

```
$ brian evolve --settle-drop round-old.json=round-new.json \
      --settle-why "the books have said otherwise three times" --settle-by alice
withdrawn      round-old.json -> round-new.json
                the decision stays in the store, with this withdrawal on 2026-11-20T04:10:53.521156+00:00
                because: the books have said otherwise three times
                withdrawn by: alice
                the contradiction it settled is back: `--audit` charges it again until a new decision is taken (boundary 292)
repair plan      nothing left the table: cost 0 -> 1, 1 file(s) still owing
```

注意最后一行：修完之后计划**变差了**（`cost 0 -> 1`）。这正是要点——撤销不是修复，它是把一笔债重新记回来。撤销
之前 `--audit --strict` 退 0，撤销之后：

```
$ brian evolve --audit --strict        # exit 2
1 replacement link(s) disagree, so the two ends of the relation cannot both be true:
  round-old.json             mismatch  round-new.json, round-old.json, round-other.json: round-old.json names
                                       round-new.json as its successor, but round-new.json says it replaced round-other.json
1 decision(s) were withdrawn by a person, and the contradiction each one settled is back:
  round-old.json             -> round-new.json              (2026-11-20T04:10:53.521156+00:00, by alice)
A withdrawal says the decision no longer stands; it does not settle anything, so the pair is charged again
until a new decision is recorded (boundary 291).
```

`--history` 的行上是短句，`--campaign` 的 `links` 上是同一条的汇总：

```
[the decision on round-new.json was withdrawn on 2026-11-20T04:10:53.521156+00:00: the pair is charged again]
links             1 decision(s) were withdrawn by a person, so the contradiction each one settled is charged again:
                  round-old.json -> round-new.json (2026-11-20T04:10:53.521156+00:00)
```

`--campaign --json-out` 里是 `settlements_withdrawn`（每条带着自己完整的 `withdrawn` 列表），`settlement_summary`
多两个数：`stored`（文件里一共几条）和 `withdrawn`（其中几条被人收回）。这两个数必须都有：「一条都没撤」和「这个字段
还不存在」要能分开（第 79/166 条边界）。

这一轮有三处是被运行逼出来的：

1. **撤销之后，计划指名的命令会拒绝自己。** `repair_plan` 的 `remedy` 原来是 `brian evolve --settle-link 老记录=后继`，
   而被撤销的那一对正是 `--settle-link` 拒绝的对象（「再记一次就是一条新决定，这个构建没有那个命令」）。一条指名的
   补救命令跑不起来，是第 261 条边界的原样重演。现在 pair 带上 `withdrawn`，`_settle_command` 在没有可撤销的对时
   改说 `brian evolve --history  (the decision on this pair was withdrawn: this build has no command for a new one)`。
2. **撤销块被塞进了 `if result.settled:` 里面。** `Audit.settled` 现在只放还站着的决定，于是一份「只有一条、而且被
   撤销了」的账本什么也不打印——页面在最该说话的时候沉默。把它移到同一个函数的下一层，并且顺手把 `settled` 的
   赋值改成 `active_settlements(...)`：`--audit` 不再一边说「已结算」一边说「已撤销」。
3. **计划那一段被缩进成了 `if result.settlements_withdrawn:` 的子句。** `campaign.py` 里 `for item in
   result.reaffirm_plan[:1]:` 原本在更深的一层，插入块写在 4 格缩进处，于是整段 `re-take next` 跑到了新 `if` 里，
   页面干脆不打印它——四条既有测试一起变红才暴露。插入脚本必须按文件里的真实缩进写锚点，不能凭记忆。

**边界 291：撤销写在被撤销的决定里，并且要和还站着的决定分开打印。** 一条决定被收回，是这条决定自己的历史；它不
该从文件里消失，也不该混在「仍然有效」的一堆里。读者要能一句话分清：哪些决定还算数，哪些是被谁在什么时候收回的。

**边界 292：撤销一条决定，会把它结算掉的矛盾还回来。** 在有人重新决定之前，那一对重新是矛盾，所以 `--strict` 只会
更严、不会更松。任何让「收回一条决定」变成「少一个缺陷」的实现，都是在教人用撤销去糊账。

**边界 293：被撤销的决定什么都不欠。** 解释、重开、再确认、删除——都不该再算在它头上。把它算进站着的决定里，页面
报的就是一笔没人能付的债；`settlement_summary` 因此把 `stored` 和 `withdrawn` 分开，`unexplained_decisions` 直接跳过它。

### 3.132 拿回来也要是一条决定：撤销与重新决定之间的那一格

第 125 轮让「这条决定是错的」变成可记录的事，读者手里于是多了一条命令，也少了一条：`--settle-link` 拒绝一条已
撤销的决定（再记一次就是新决定），`--settle-reaffirm` 也拒绝它（「重开会唤回一份记录说已经不算的东西」）。两条
路都堵着，而那一对又因为撤销重新成了矛盾、被 `--strict` 收着费。唯一的出路是手改 `settlements.json`——正是第
83/112 轮存在的原因。这个循环在「收回」和「重新决定」之间少了一格。

第 126 轮补的是这一格：

```
brian evolve --settle-restore RECORD=SUCCESSOR --settle-why TEXT [--settle-by NAME]
```

它往这条决定自己的 `redecisions` 列表里追加 `{at, why, by}`，**不动** `withdrawn`，于是账本上一对决定同时写着两
件事：「因X于某时被收回」和「因Y于某时被重新决定」。判断「这一对现在还作不作数」的，是**最后发生的那件事**——
`is_withdrawn` 从「是否曾经被撤销」改为「最新的撤销是否晚于最新的重新决定」。这一改，`replacement_links`、
`provenance_block`、`blocked_pairs`、`settlement_summary`、`unexplained_decisions`、`frequent_retakes` 一个字都没动
就都跟着对了：它们本来问的就是「这一对现在算不算解决了」。

`var/demo126`（由 `var/demo125` 复制，那一对正是上一轮被撤销的）：

```
$ brian evolve --audit --strict                      # exit 2
1 decision(s) were withdrawn by a person, and the contradiction each one settled is back:
  round-old.json             -> round-new.json              (2026-11-20T04:10:53.521156+00:00, by alice)

$ brian evolve --settle-restore round-old.json=round-new.json \
      --settle-why "the third claim is dated after the decision, and the retirement mark is still the surviving one" \
      --settle-by bob
taken up again  round-old.json -> round-new.json
                the decision stands again from 2026-11-20T04:28:03.058999+00:00, and the withdrawal stays on record
                because: the third claim is dated after the decision, and the retirement mark is still the surviving one
                restored by: bob
                the contradiction it settles is muted while it stands: `--audit` stops charging that pair, and
                `--settle-drop` takes it back if this decision turns out wrong too (boundary 294)
repair plan      1 file(s) off the table: data/smd/stream-real.json -- cost 1 -> 0

$ brian evolve --audit --strict                      # exit 0
1 decision(s) carry a withdrawal a later decision answered:
  round-old.json             -> round-new.json              (withdrawn 2026-11-20T04:10:53.521156+00:00, taken up
                                                             again 2026-11-20T04:28:03.058999+00:00, by bob)
```

注意最后一段的位置：它**不是**「已结算」，也不在「被撤销」那一块里。一对被做过两次决定（中间撤回过）和一对只做过
一次，是不同的历史，所以 `--audit` 单开一块，`--campaign` 单开一行，`--history` 的行上把两个时刻都写出来：

```
[withdrawn on 2026-11-20T04:10:53.521156+00:00, taken up again on 2026-11-20T04:28:03.058999+00:00]
links             1 decision(s) were taken up again by a new decision: round-old.json -> round-new.json
                  (withdrawn …, taken up again …)
```

`--campaign --json-out` 里是 `settlements_restored`（每条带完整的 `withdrawn` 与 `redecisions`），`settlement_summary`
多一个 `redecided`。这是第三个数：`stored`（一共几条）、`withdrawn`（现在撤着的几条）、`redecided`（曾经被收回、
后来又被重新决定的几条）——三个数缺一个，「从来没发生过」和「发生过又反悔了」就分不开（第 166/295 条边界）。

四处是被运行逼出来的：

1. **计划指名的补救命令又撒了一次谎。** 第 125 轮把 `_settle_command` 改成在没有可撤销对时说「这个构建没有新决定的
   命令」；这一轮把那个命令建了出来。计划于是反过来教人「没有命令可用」——和第 261 条边界是同一条规则的另一半：
   **一个句子说某条命令不存在时，它必须是真话**。现在 `_settle_command` 先看有没有被撤销的对，有就指名
   `--settle-restore RECORD=SUCCESSOR --settle-why TEXT --settle-by NAME`；`settle_link` 的拒绝也改成指名它。
2. **幂等检查排在了拒绝之后。** 第一次重新决定之后，这一对就「站着」了，于是同一条命令再跑一次会撞上「没有东西可
   拿回来」的错误，而不是安静地什么也不做。幂等必须排在前：重复执行一条命令不是一次新的错误。
3. **行上的标记又落到了后继那一行。** 一对决定同时点名两端，标记不检查自己站在哪一端，于是 `round-new.json` 那行也
   写着「被撤销、又被拿回来」——一个从来不是决定的轮次被印成了决定。第 124 轮给「反复重开」修过同一处，这次的条件
   是 `own_name == pair[0]`。修之前 `--history` 的两行各印了一遍，读起来像两条决定。
4. **测试夹具的时钟又领先了机器。** `NOW` 是 2026-12-01，比机器现在晚，所以 CLI 用 `datetime.now()` 记下的重新决定
   比夹具里的撤销**更早**，`is_withdrawn` 仍然为真。CLI 路径的夹具必须用 `_ago` 落在过去（第 119/120/122 轮同一坑）。

**边界 294：决定是唯一被允许让矛盾安静下来的东西，而且页面上必须说出来。** 拿回一条决定会让那一对重新不被收费——
这正是决定该有的效果，所以 `--settle-restore` 的输出要写明「它安静下来了」，并指名反悔的命令（`--settle-drop`）。
任何让「恢复一条决定」看起来像「修好一个缺陷」的实现，都是在教人用撤销与恢复交替糊账：`--strict` 会因为一个人的
一句话变绿，而这必须被看见，不能混在「已修复」里。

**边界 295：一对决定被做过两次，和只做过一次，是不同的历史。** 中间撤回过的那一对，读者要能查到两个时刻、两条理由、
两个署名：`redecided` 数全库，`settlements_restored` 带全文，`--history` 的行上写两个时刻，`--campaign` 单开一行。

**边界 296：拒绝与补救的说法必须跟着命令一起改。** 判词说「没有这个命令」而命令已经存在，和指名一个会拒绝的命令，
是同一个错误的两个方向（第 261 条边界的另一半）。加一条命令、删一条命令，都要回头把引用它的句子找一遍——这一轮
`--settle-link` 的拒绝、计划里的 remedy、文档里的 item 文本三处都跟着改了。


### 3.133 撤销之后没人接手的那些天，是唯一能收费的那一段

第 125 轮把「这条决定不算了」写进了记录，并且让那一对重新成为矛盾——于是它被 `--strict` 以 exit 2 收着费（边界 292）。第 126 轮补上了「重新决定」这条命令。两轮之后仍然缺一件小事：**撤销本身有多久了，没人说**。

不是记账上的小事。`settlements.json` 里一条被撤销的决定和一条三天前被撤销的决定长得一样，页面只数「有几条被撤销」；读者因此无法回答「我今天该先做什么」，而第 111 轮刚刚教会循环「一条没人解释的决定是一项可以收费的债」——撤销是同一类债，而且它的补救命令从第 126 轮起**确实存在**，这就满足了收费的条件（边界 266 的另一半）。

`unanswered_withdrawals(settlements, now=None)` 遍历仍在撤销状态的决定，从**最新一次**撤销的时刻起算天数，最老的排前面，带上当时的理由与署名：

```
$ brian evolve --audit
1 decision(s) were withdrawn by a person, and the contradiction each one settled is back:
  round-old.json             -> round-new.json              (2026-11-16T04:45:53.032630+00:00, by alice)
A withdrawal says the decision no longer stands; it does not settle anything, so the pair is charged again until a new decision is recorded (boundary 291).
1 withdrawal(s) have no answer yet: round-old.json -> round-new.json (withdrawn 2026-11-16T04:45:53.032630+00:00, 4.0 day(s) ago)
`--settle-restore RECORD=SUCCESSOR --settle-why TEXT --settle-by NAME` decides one again: the withdrawal stays on the record, and the pair stops being charged (boundary 297).
```

两点：**年龄与命令印在同一页**（边界 297），**年龄在有没有上限时都印**（第 117 轮的规矩，用在这一类债上）。上限是政策，年龄是事实；把政策关掉不该让事实消失。

```
$ brian evolve --audit --strict                        # exit 0
$ brian evolve --audit --strict --max-withdrawn-days 3 # exit 1
1 decision(s) have been withdrawn for longer than the 3 day(s) asked for:
  each of those pairs is a contradiction nobody has ruled on: the withdrawal says the decision no
  longer stands, and nothing has taken its place, so `--audit` charges it and
  `--settle-restore RECORD=SUCCESSOR --settle-why TEXT --settle-by NAME` is the command that pays it:
  the pair is decided again, and the withdrawal stays on the record (boundary 297).
```

`--history` 与 `--campaign` 各有一行，campaign 那行指名最老的一条和它自己的命令：

```
withdrawn       1 decision(s) were withdrawn and nobody has taken them up again:
  round-old.json -> round-new.json  (withdrawn 2026-11-16T04:45:53.032630+00:00, 4.0 day(s) ago)
links    1 withdrawal(s) have no answer: round-old.json -> round-new.json was taken back on 2026-11-16T04:45:53.032630+00:00 (4.0 day(s) ago) and nothing has replaced it -- start with brian evolve --settle-restore round-old.json=round-new.json --settle-why TEXT --settle-by NAME
```

`--settle-restore` 之后同样的命令 exit 0，`--audit` 换成 `1 decision(s) carry a withdrawal a later decision answered:`：收费消失，撤销仍在。

**演示逼出来的一个区别**（这是本轮真正的发现）：一开始的演练账本里，那一对既是「被撤销」又是 `mismatch`，`--strict` 早就以 exit 2 收着费了——**年龄收的是第二笔钱**，而缺陷只有一个。年龄收费真正能做的事只在一个状态里：**账本已经不再互相矛盾，但没人重新决定**（那条记录的后继改指别处、其它宣称者被 retire 掉，撤销孤零零地留着）。这个状态是真实的，而且恰好是最容易被忽视的：矛盾消失让人以为问题解决了，其实只是没人再提。所以演示把矛盾先化开，再让年龄收费；两种状态在页面上必须说得出区别（边界 298）。

**边界 297**：一项债的页面必须同时印出它的年龄与能清掉它的那一条命令——只写「有 N 条在等」，读者拿到的是一句抱怨，不是一件工作。
**边界 298**：同一条缺陷只能收一笔钱：撤销让矛盾回来了，链规则已经在 exit 2 上收费，年龄不得再收一次；年龄收费属于「账已经和好、却没人裁定」的那个状态，页面必须让这两种状态分得开。

### 3.134 同一笔欠账，按条算和按文件算不是一件事

第 127 轮让「被撤销、没人接手」这件事有了年龄，并进了闸门。但它的上限是按**条数**说的：`--max-withdrawn-days`
量的是「多久没人管」，`--max-withdrawn-per-file` 量的是「一个文件上堆了几条」。这两件事看起来是一件事，
其实不是。同一笔债放在十一个文件上，是十一个下午；十一条债堆在同一个文件上，是一个下午——同样的钱，
两种活法。第 121 轮已经在「移动过的决定」上吃过这个亏（边界 279），这里只是把同一条规矩用到撤销上。

`src/brian/evolve.py` 的 `withdrawal_plan(rounds, settlements, now=None)` 把 `unanswered_withdrawals` 的行
按**记录自己的 `stream_path`** 分桶（不是按它现在指向哪儿），每桶给三件事：几条、最老一条多大，以及
`brian evolve --settle-restore <record>=<successor> --settle-why TEXT` 这条命令。桶按「条数降序、文件名字典序」
排——一个每次跑都换顺序的页面教人别信它（边界 246）。分桶要读 `rounds`，所以 `unanswered_withdrawals` 多了
一个可选参数 `rounds`：不给就没有 origin，给了才填。

演练账本：`data/smd/stream-real.json` 上一条（老 3 天），`data/smd/stream-two.json` 上两条（老 2 天）。

```
$ brian evolve --campaign
links             withdrawn and waiting, by stream file: data/smd/stream-two.json 2 decision(s); data/smd/stream-real.json 1 decision(s)
```

闸门只在一个文件的条数超出上限时才收费，并把文件名说出来：

```
$ brian evolve --audit --strict                              # exit 0
$ brian evolve --audit --strict --max-withdrawn-days 5       # exit 0，没有超过 5 天的
$ brian evolve --audit --strict --max-withdrawn-per-file 2   # exit 0
$ brian evolve --audit --strict --max-withdrawn-per-file 1   # exit 1
2 withdrawal(s) sit on one stream file, above the 1 asked for for a single file:
  the file is data/smd/stream-two.json, and each of those pairs is one nobody has ruled on since it was taken back.
  A cap in decisions says the debt is large; a reader pays in afternoons, and eleven withdrawals on
  eleven files is eleven of them while eleven on one file is one (boundary 279, applied here).
  `--settle-restore RECORD=SUCCESSOR --settle-why TEXT` decides one again, and the withdrawal stays
  on the record: --strict exits 1 until this file's count is back inside the cap (boundary 299).
```

同一份账本上，`--max-withdrawn-days 5` 判 0、`--max-withdrawn-per-file 1` 判 1——两个上限回答的是两个问题，
所以各自打各自的判决（边界 281 的第三次应用）。年龄上限说「这些债放了多久」，文件上限说「今天下午够不够」；
一个读者手里只有下午，问的是后者。

两处实现细节值得写下来：

- `_strict_exit` 是模块级函数，看不到 `cmd_evolve` 的局部变量，所以文件名以 `unanswered_file` 参数传进去——
  第一版直接把 `_worst_withdrawn` 写在函数体里，`pyflakes` 报 `undefined name`。
- `unanswered_withdrawals` 的 `origin` 来自**账本行**（第 34 轮：账本行覆盖轮记录文件）。演练里把轮文件的
  `stream_path` 改成 `stream-real.json` 时，账本行还写着 `stream-one.json`，于是分桶落到了记录从未命名过的文件名下
  ——两本书都要改，或者干脆用 `retire()`（第 120 轮同一个坑）。

边界：

- **299** 同一笔债换一个计量单位，就得分一个新的上限：按条收的是「债大不大」，按文件收的是「一个下午够不够」；
  两个都收，才不会出现「条数没超、文件全堆在一处」的假安全。
- **300** 计划里每一桶都必须带一条能付掉**整桶**的命令：读者不会照着 N 条记录敲 N 次理由，而理由敲两次就开始
  走样（第 120 轮）。

### 3.135 按文件排的队，就得能用一条命令付掉

第 128 轮把撤销欠账按流文件分了桶，每桶给出一条命令。但那条命令是**单对**的——`--settle-restore A=B`。
于是读者读完「这个文件上欠两条」，仍然要敲两次理由。分桶本来是为了让人按下午安排工作，结果省下的是分组、
没省下的是重复劳动：一份按文件分组、却要逐条执行的计划，等于没分组（边界 301）。

`--settle-restore-file STREAM` 补上这一格，形状与第 122 轮的 `--settle-reaffirm-file` 完全对称：从
`withdrawal_plan` 取出该文件的全部待办对，再**递归**交给 `--settle-restore` 分支（这一轮把它改成 `nargs="+"`）。
递归不是省事的写法，是唯一能保证两条命令规则一致的做法——逐对输出、被拒一对不打断其余（边界 277）、
`--json-out` 是列表而不是随参数个数变形的形状（边界 278），这些都只有一个实现（边界 284）。

演练账本延续 §3.134：`stream-real.json` 上一条、`stream-two.json` 上两条。第 128 轮以后，页面给出的命令已经是付文件的那条：

```
$ brian evolve --campaign
links             withdrawn and waiting, by stream file: data/smd/stream-two.json 2 decision(s); data/smd/stream-real.json 1 decision(s)
links             3 withdrawal(s) have no answer: round-old.json -> round-new.json was taken back on … (3.0 day(s) ago) and nothing has replaced it -- start with brian evolve --settle-restore-file data/smd/stream-two.json --settle-why TEXT --settle-by NAME

$ brian evolve --settle-restore-file data/smd/stream-two.json \
      --settle-why "both claims are dated after their decisions" --settle-by alice
re-taking      2 decision(s) on data/smd/stream-two.json in one command
taken up again  round-ninth.json -> round-tenth.json
                the decision stands again from 2026-11-20T05:02:06.733623+00:00, and the withdrawal stays on record
                because: both claims are dated after their decisions
                restored by: alice
                the contradiction it settles is muted while it stands: `--audit` stops charging that pair, and
                `--settle-drop` takes it back if this decision turns out wrong too (boundary 294)
taken up again  round-sixth.json -> round-seventh.json
                …
re-taken       2 of 2 decision(s), 0 was already standing, 0 refused
repair plan      nothing left the table: cost 0 -> 0, 0 file(s) still owing
```

付掉一整个文件之后，页面对剩下那一条自动回落到单对命令——不是特例代码，是同一句话里的同一个选择：

```
links             1 withdrawal(s) have no answer: round-old.json -> round-new.json … -- start with brian evolve --settle-restore round-old.json=round-new.json --settle-why TEXT --settle-by NAME
```

再对那个已经清空的文件跑一次，命令拒绝而不是静默成功：

```
$ brian evolve --settle-restore-file data/smd/stream-two.json --settle-why x
no unanswered withdrawal is recorded against data/smd/stream-two.json: `brian evolve --campaign` lists the files that hold one
```

三处实现细节：`--settle-restore` 由单值变 `nargs="+"` 后，`--json-out` 从对象变成 `{"restored": [...], "restore_refused": N}`，
两条既有测试因此要改（一批数据的形状不能取决于敲了几个参数，边界 278）；加 `--settle-restore-file` 分支时必须放在
`--settle-restore` **之前**，否则空参数会让 `--settle-restore` 先报「没有 = 」；递归调用要清掉 `settle_restore_file`
本身，否则无限递归。

边界：

- **301** 按文件排的队必须能用一条命令付掉：一条按文件分组、却打印单对命令的计划，把省下的分组又还回去了。
- **302** 批量命令不该自己实现一遍单条规则；递归复用同一个分支，拒绝策略、输出形状、JSON 形状才只有一份（第 284 轮的重申）。
- **303** 批量里被拒的一对不能打断其余（边界 277），而且最后必须给出「几对成功、几对被拒」的总数——一份逐对输出而没有合计的
  记录，等于让读者自己数。

### 3.136 批量命令要先能看，后能跑

第 83 轮给 `--repair-links` 立过一条规矩：会改历史记录的命令，必须能先看一眼再动手。那条规矩到第 130 轮才轮到
批量命令——而批量命令恰恰是最需要的：它一次写 N 条决定，跑之前唯一能知道「会动哪几条」的办法就是跑一遍，
而跑一遍就意味着理由已经写进去了。

`--dry-run` 只对 `--settle-reaffirm-file` 与 `--settle-restore-file` 生效，输出的是同一份计划里的同一批对
（边界 283：页面印的清单，就是命令要跑的清单）：

```
$ brian evolve --settle-restore-file data/smd/stream-two.json \
      --settle-why "both claims are dated after their decisions" --settle-by alice --dry-run
would take up  2 decision(s) on data/smd/stream-two.json in one dry run: nothing was recorded
  round-ninth.json=round-tenth.json  (withdrawn 2026-11-18T05:08:40.317322+00:00, 2.0 day(s) ago)
  round-sixth.json=round-seventh.json  (withdrawn 2026-11-18T05:08:40.317322+00:00, 2.0 day(s) ago)
nothing was written; afterwards that file would owe no unanswered withdrawal. Drop --dry-run to take them up again (boundary 304).

$ brian evolve --settle-reaffirm-file data/smd/stream-one.json --settle-why "read today's books" --dry-run
would re-take  3 decision(s) on data/smd/stream-one.json in one dry run: nothing was recorded (2 never re-taken, 1 re-taken before and moved again)
  round-fourth.json=round-new.json  (never)
  round-old.json=round-new.json  (again)
  round-third.json=round-new.json  (never)
nothing was written; drop --dry-run to re-take them (boundary 304).
```

`--settle-kind` 在干跑里照旧生效（它决定的是「这次要付哪一种活」，与写不写无关），被留下的那一族照样打印
`leaving N decision(s) of the other kind alone`。文件上什么都没有时，拒绝先于预览：`no unanswered withdrawal is
recorded against …`，exit 2——干跑不是「什么都答应」。

`--dry-run` 落在别的命令上直接 exit 2，而不是被安静忽略：

```
$ brian evolve --audit --dry-run
--dry-run previews --settle-reaffirm-file or --settle-restore-file; without one of those the command would go ahead and record nothing you could see first
```

这一轮还顺手修掉第 129 轮的一处不对称：`--settle-restore-file` 递归时把 `inner.json_out` 清空了，于是批量付了
N 条决定却一个 payload 也不写，而 `--settle-reaffirm-file` 会写。「干跑什么都不写」与「真的跑了要交代」是两件
义务，前者靠提前 return，后者靠把参数透传下去（边界 306）。修完之后 `--json-out` 拿到
`{"restored": [...], "restore_refused": 0}`。

边界：

- **304** 批量命令必须先能看后能跑：一次写 N 条记录的命令，若只能用「跑一遍」来预览，那预览已经把理由写进去了。
- **305** 安全阀不能被安静忽略：`--dry-run` 落在不读它的命令上必须报错退出，否则它看起来像系好了安全带。
- **306** 干跑什么都不写、真跑必须交代：两者不能靠同一个开关实现，前者是提前 return，后者是把输出参数传下去。

### 3.137 干跑要说「跑完之后账变成什么样」

第 130 轮的干跑回答了「会动哪几对」，没回答「值不值得跑」：读者仍要跑一遍才知道这个文件是不是就此不欠了。
而这个问题是可以提前算的——settlements 就是一个列表，两个计划（`withdrawal_plan`、`reaffirm_plan`）又都是它的
纯函数，于是「付款后的副本」可以构造出来，一个字节都不用写：

```
$ brian evolve --settle-restore-file data/smd/stream-two.json \
      --settle-why "both claims are dated after their decisions" --settle-by alice --dry-run
would take up  2 decision(s) on data/smd/stream-two.json in one dry run: nothing was recorded
  round-ninth.json=round-tenth.json  (withdrawn 2026-11-18T05:12:13.025936+00:00, 2.0 day(s) ago)
  round-sixth.json=round-seventh.json  (withdrawn 2026-11-18T05:12:13.025936+00:00, 2.0 day(s) ago)
after this run  data/smd/stream-two.json would owe 0 unanswered withdrawal(s), and the ledger would go from 3 to 1
                the repair bill is not previewed: it reads the queue's stored refusals, which this command does not
                touch -- run `--campaign` afterwards to see it (boundary 307)
```

真跑一次核对：`re-taken 2 of 2 …`，之后 `--campaign` 只剩 `1 withdrawal(s) have no answer`——预览说 3→1，账上就是 3→1。
重开那一族也一样：`--settle-reaffirm-file … --dry-run` 说「这个文件 0 条、全局 5→2」，真跑之后页面只剩另一个文件上的两条。

实现只有一处新东西：`preview_settlement_change(settlements, pairs, *, kind, now, why, by, claimants)` 返回
**列表的副本加上每对一个条目**（`redecisions` 或 `reaffirmations`），不碰磁盘。`is_withdrawn` 读的是两侧时刻的
先后（第 126 轮），所以「加上一条 redecision」在副本里就已经把「仍然撤销中」变成「已拿回」，不需要第二套判断规则。

两面都要守住：

- 预告的数字必须来自**同一份推算**，不是另写一套近似：`after_rows = withdrawal_plan(rounds, after)` 与页面上那一行
  调的是同一个函数，只是喂了另一份列表（边界 308）。
- 算不出来的那一行必须**说它没算**，而不是留白或者猜：修复账单读的是 `blocked_dossier`（队列里存下的拒绝句子），
  与 settlements 无关，所以预览只配一句「跑完再看 `--campaign`」。一份只报好消息的预览，比没有预览更危险。

边界：

- **307** 预览必须声明它的作用域：能算的算准，算不了的点名，不然「预览说没问题」会被读成「跑完没问题」。
- **308** 预告与事后必须共用同一份推算：另写一套近似，预言迟早与账本分叉，而分叉之后没人知道该信哪一个。

### 3.138 这个问题属于页面，不属于那条命令

第 131 轮把事后账算出来了，但它只挂在干跑后面：读者看着页面上的「这个文件欠两笔」，仍要先想出 `--settle-restore-file`
这个名字，才能问出「付掉会怎样」。问题本身是关于页面的，所以这一轮把它放到页面上：

```
$ brian evolve --campaign --what-if-records data/smd/stream-two.json
what if          data/smd/stream-two.json owes 2 unanswered withdrawal(s) and 3 stale decision(s)
                 paying the withdrawals (2 pair(s), `--settle-restore-file data/smd/stream-two.json`):
                   after this run  data/smd/stream-two.json would owe 0 unanswered withdrawal(s), and the ledger would go from 3 to 1
                 paying the re-takes (3 pair(s), `--settle-reaffirm-file data/smd/stream-two.json`):
                   after this run  data/smd/stream-two.json would owe 2 stale decision(s), and the ledger would go from 5 to 4
                 nothing here is written: this is the same recomputation the dry runs print (boundary 308)
```

**两族分开问**：付掉撤销不清掉重开，合并成一个数字就没人知道哪一半付了。所以页面对同一个文件问两次，各自给一行事后账。

**这一轮最有价值的一行是「付完还欠」**。上面第二个问题说：把该文件 3 对重开全部付掉，它仍然欠 2 笔。当时只当是
推算的边界，真跑一次对上了：

```
$ brian evolve --settle-reaffirm-file data/smd/stream-two.json --settle-why "read today's books" --settle-by alice
re-taking       3 decision(s) on data/smd/stream-two.json in one command (2 never re-taken, 1 re-taken before and moved again)
re-taken       1 of 3 decision(s), 0 kept its date (already re-taken), 2 refused
```

两对被拒，原因写在 `reaffirm_link` 里：`round-sixth.json` 现在指向 `round-seventh.json`，再确认「它指向 round-new」
就是在断言账本否认的事（第 119 轮的守卫，正确地拒绝了）。于是「付完仍欠 2」不是推算误差，而是**计划列了两条命令
自己会拒绝的对**——清单印什么，命令就跑什么（边界 261/301）在计划层被违反了。这一条留给第 119 项。

**页面不写任何东西**：`--what-if-records` 只读两套计划（`withdrawal_plan`、`reaffirm_plan`）与 settlements 副本，
`--campaign` 的其它输出一字不动；拿掉 `--campaign` 直接用它，报错退出而不是静默忽略：

```
$ brian evolve --audit --what-if-records data/smd/stream-one.json
--what-if-records asks the campaign page a question; without --campaign nothing prints it
```

边界：

- **309** 预览必须能说出「付完还欠多少」：只会说「付完就好了」的预览，是在替读者做它不该做的判断；真实的守卫、
  被拒的对、尚未到期的对，都应当在那行数字里体现。
- **310** 计划里不该出现命令自己会拒绝的对：清单与命令共用一份计划（283），所以计划若收录了跑不通的对，
  页面给出的下一步就成了「照做然后失败」（留作第 119 项）。

### 3.139 清单不能提供命令会拒绝的活

第 132 轮末尾那句「付完还欠 2」用真跑对过一次：`re-taken 1 of 3 decision(s), 0 kept its date (already
re-taken), 2 refused`。两对被拒不是算错——`round-sixth.json` 已经指向 `round-seventh.json`，
`reaffirm_link` 拒绝断言账本否认的事（第 119 轮），拒绝得对。错的是**清单**：`reaffirm_plan` 把这两对算
成 `stale`，页面据此印出数字和命令，命令一对一对拒。清单印什么、命令跑什么（边界 261/301），这一条在计划
层被违反了。

修法是把「能不能付」变成计划自己的问题：

- `_named_successor(record)` 抽出 `reaffirm_link` 拒绝前问的同一问题（这条记录今天把谁写作后继）；
- `reaffirm_plan` 的每个 bucket 多出 `unpayable` / `unpayable_pairs` / `stuck`，不可付的对**不进** `stale`
  / `pairs` / `commands`，而是进 `unpayable`，并带上真能执行的补救命令 `--settle-drop`；
- 行的过滤条件变成 `stale or unpayable`——只欠不可付的活也必须出现在页面上；
- 排序变成 `(-stale, -unpayable, origin)`。

后果是自动的：`--settle-reaffirm-file` 的 `pairs` 来自 `row["pairs"]`，于是批量命令的头行数字
（`X never re-taken, Y re-taken before and moved again`）与它真的要做的完全一致；`--max-moved-per-file`
读 `stale`，只欠不可付活的文件不再被收费（边界 287 落到计划层）。

```
$ brian evolve --campaign
links             2 stale decision(s) on data/smd/stream-two.json cannot be re-taken at all: round-other.json ->
                  round-new.json (the record now names round-elsewhere.json) --
                  brian evolve --settle-drop round-other.json=round-new.json --settle-why TEXT
links             re-take next: data/smd/stream-one.json -- 2 stale of 2 recorded decision(s), ...

$ brian evolve --settle-reaffirm-file data/smd/stream-two.json --settle-why "read today's books" --dry-run
would re-take  1 decision(s) on data/smd/stream-two.json in one dry run: nothing was recorded (1 never re-taken,
               0 re-taken before and moved again)
  round-fifth.json=round-new.json  (never)
```

第 132 轮真跑那份文件时，头行说 `3 decision(s)`，跑完是 `1 of 3 … 2 refused`；现在头行说 `1 decision(s)`，
跑完是 `1 of 1`。**页面的数字第一次是承诺，而不是愿望。**

#### 第三个发现（跑出来的）：撤销过的决定不是「欠一次重开」

按页面点名的补救命令走一遍：`--settle-drop round-other.json=round-new.json` 记账成功、无答复撤销数 3 → 4，
然后页面**仍然**印：

```
links             2 stale decision(s) on data/smd/stream-two.json cannot be re-taken at all: round-other.json ->
                  round-new.json (the record now names round-elsewhere.json) -- ...
```

原因：`moved_settlements` 从没学过「撤销」这件事。第 125 轮把撤销接进了 `replacement_links`、
`settlement_summary`、`frequent_retakes`、`unexplained_decisions`、`reaffirm_link`、`settle_link`，唯独漏了
这里。于是一笔已被撤回的决定被算成「欠一次重开」，而重开命令会因为**另一个理由**（决定已经不存在）拒绝
它——和这一轮开头那类错误同一个形状，只是高了一层。第 125 轮的边界 293 早就说了「被撤回的决定什么都不
欠」，这一轮让计划同意它：`moved_settlements` 跳过 `is_withdrawn(item)`。

修完之后同一串命令变成 2 → 1 → 消失，剩下的账老老实实转到撤销队列里（无答复撤销 5 笔），而那条队列有自己
的命令和年龄。

**边界 311**：清单只能数「命令真能做」的活——否则页面的数字是愿望，命令的拒绝看起来像缺陷。
**边界 312**：命令付不了的活要被报告，不被收费（154/171/266/271 的第六次，这次在计划层）。
**边界 313**：不可付的活给出的补救必须是一条真能跑的命令；这里是 `--settle-drop`，它把「欠一次重开」变成
「欠一次决定」，后者有队列、有年龄、有文件分组。
**边界 314**：撤回来的决定不是待办的活——撤销与「账本变了」是两个事实，混在一起时计划会派发一个必然被拒
的命令，而页面看不出它为什么被拒。

第 133 轮：1902 tests，pyflakes 0；`var/demo133` 里 2 → 1 → 消失三段实录。


### 3.140 一条命令付一整个文件，以及「为什么付不掉」要写在那一行上

第 133 轮把不可付的对从 `stale` 里分离出来，`--campaign` 单独一行点名，补救命令写成
`--settle-drop round-other.json=round-new.json --settle-why TEXT`。那句话是真的，但一个文件欠三对时要打三遍同
一个理由——而理由每打一遍都有机会变得不一样（这正是第 133 轮全部论证的反面）。这一轮把它做成和
`--settle-restore-file` / `--settle-reaffirm-file` 同形状的第三个批量命令：

```
$ brian evolve --settle-drop-file data/smd/stream-two.json --settle-why "both records name other successors now" --settle-by bob
dropping       2 decision(s) on data/smd/stream-two.json in one command
withdrawn      round-other.json -> round-new.json
                because: both records name other successors now
                withdrawn by: bob
                the contradiction it settled is back: `--audit` charges it again until a new decision is taken (boundary 292)
withdrawn      round-sixth.json -> round-new.json
                because: both records name other successors now
                withdrawn by: bob
                no contradiction is waiting on that pair: the decision simply stops applying
withdrawn      2 of 2 decision(s), 0 already withdrawn, 0 refused
```

三件事照着前两族的规矩办：对子从 `--campaign` 印的同一份计划里取（边界 283），走的是单对分支（边界 284），
一对被拒不影响其余（边界 303），JSON 载荷永远是一个列表（边界 278 的第四次应用）。所以 `--settle-drop` 的
载荷也从对象变成列表 `{"withdrawn": [...], "drop_refused": N, "drop_skipped": N}`——一个随参数个数变形的载荷，
是每个读者都要分支处理的类型。

#### 第二个发现：读者不必先跑到另一页才知道自己看的是一条付不掉的账

`--history` 的行里原本只有「已按决定结算」和「被重开过几次」，至于这一对**能不能被重开**，只有 `--campaign`
说。读者从 `--history` 打开一条记录，看到的是一句平静的 `[settled with round-new.json by a recorded decision]`，
而那条决定早就指不动了。现在行上多一句，用的还是计划那份计算（边界 264/308，不另写一套规则）：

```
 7  2026-11-18T07:05:00+00:00  …  [retired, re-measured as round-seventh.json]  [data/smd/stream-two.json]
    [settled with round-new.json by a recorded decision]  [cannot be re-taken: the record names round-seventh.json]
```

同一行还能同时挂着 `[the decision on round-seventh.json was withdrawn …]`——那是**另一条**决定被撤回，两句都是
真的，只是挤在一行。这一轮没把行拆开：拆行是排版问题，而边界 241 已经讲过「一行只放属于这一行的事实」。

**边界 315**：读者要据此行动的事实，必须写在他正在看的那一行上——只在另一页出现的「这条付不掉」，等于让读者
先怀疑再验证。
**边界 316**：一个必须逐项敲的命令，会把一个理由变成 N 个理由；批量不是省事，是让理由只有一份。
**边界 317**：文件形状的命令必须从同一份计划取对子、走同一条单对分支、把拒绝数进总数——三族命令一个形状，
读者才不用为每个族记一套语法。

第 134 轮：1911 tests，pyflakes 0；`var/demo134` 里一条命令付掉一个文件的 2 对不可付决定，`var/demo134b`
留下行内那句「为什么付不掉」。


### 3.141 三条批量命令，就这一条不能先看：预览的口子要跟着家族长

第 130 轮给两条批量命令装了 `--dry-run`（`--settle-reaffirm-file` 与 `--settle-restore-file`），护栏写得很清楚：
这两个之外用 `--dry-run` 一律拒绝，理由是一句照旧的话——"看起来像安全带的开关不能是装饰品"（边界 305）。
第 134 轮加了第三条批量命令 `--settle-drop-file`，护栏没有跟着长。于是同一个开关，在两个兄弟身上是预览，
在最小的弟弟身上是拒绝：

```text
$ brian evolve --settle-drop-file data/smd/stream-two.json --settle-why "..." --dry-run
--dry-run previews --settle-reaffirm-file, --settle-restore-file or --settle-drop-file; without one of those the
command would go ahead and record nothing you could see first
```

护栏本身是对的（它没有假装预览），错的是它数错了家族。改法不是放宽判断，而是把 `settle_drop_file` 加进
那个条件，然后让这条命令真的会预览：

```text
would withdraw 2 decision(s) on data/smd/stream-two.json in one dry run: nothing was recorded
  round-other.json=round-new.json  (the record now names round-elsewhere.json)
  round-sixth.json=round-new.json  (the record now names round-seventh.json)
  after this run  data/smd/stream-two.json would owe 0 decision(s) no re-take can pay, and 2 more withdrawal(s)
                  would wait in the withdrawal queue (the queue goes from 3 to 5)
  dropping a decision does not settle anything: the pair becomes an unanswered withdrawal, which has its own
  queue, age and file grouping
nothing was written; drop --dry-run to withdraw them (boundary 304).
```

第二句"账去哪了"是本轮真正的收获。第 131 轮定下的规矩是"预览要说出跑完之后账变成什么样"（边界 307/308），
而丢弃这件事的后果不是"少了 2 条"，是"2 条从'不能重开'搬到'没人接手'"：同一个文件上的欠账条数只增不减，
只是换了队列。预览若只说"会少 2 条"，读的人会以为债务消失了——这正是第 127 轮"同一笔债不要收两次费"的反面：
也不要把一笔债说成凭空消失。`preview_withdrawal_change()` 走的是同一条路：拷贝一份 store，把 `withdrawn`
往上追加，然后让 `reaffirm_plan` 与 `withdrawal_plan` 自己重算——所以预览与真跑共用 `is_withdrawn` 一个判断。

同一天还修了另一半：`--campaign --what-if-records` 在第 132 轮只问两个家族（重开、接手），第 133 轮把
"不能重开的对"从 `stale` 里移走之后，这个问题就悄悄变成了"付得起的那部分"。页面上的答案是假的：

```text
$ brian evolve --campaign --what-if-records data/smd/stream-two.json
what if          data/smd/stream-two.json owes 2 unanswered withdrawal(s), 1 stale decision(s) and 2 decision(s)
                 no re-take can pay
                 paying the withdrawals (2 pair(s), `--settle-restore-file ...`):
                   after this run  ... would owe 0 unanswered withdrawal(s), and the ledger would go from 3 to 1
                 paying the decisions no re-take can pay (2 pair(s), `--settle-drop-file ...`):
                   after this run  ... would owe 0 decision(s) no re-take can pay, and 2 more withdrawal(s) would
                   wait in the withdrawal queue (the queue goes from 3 to 5)
                 paying the re-takes (1 pair(s), `--settle-reaffirm-file ...`):
                   after this run  ... would owe 0 stale decision(s), and the ledger would go from 3 to 2
                 nothing here is written: this is the same recomputation the dry runs print (boundary 308)
```

三个家族各算各的，都用同一个 `_after_preview_lines()`，所以页面的预言和命令的结果不会分叉。真跑一次之后，
文件上"不能重开"的那行消失，没人接手的从 3 变成 5——与预览说的一致。

**边界 318：丢弃一条决定不是解决任何事，它只是把债务换了一种形式（"不能重开"→"没人接手"），所以预览必须
说出它去哪，而不是只说出少了多少。**

**边界 319：说"把这些都付了会怎样"时，必须把别的命令拒绝的那部分算进来，否则这句话回答的是一个子集，
而读的人会当成全集。**

**边界 320：一个列出了自己所适用命令的护栏，在家族长大的那一刻就过期了；加一条命令不是加一个分支，
是把每一处点名的地方都改一遍。**

测试：`tests/test_drop_preview.py`（7 项）覆盖预览不写盘、两张账的前后数字、真跑后该家族清空且没人在
"没人接手"里补齐、`preview_withdrawal_change()` 不改 store，以及 what-if 三个家族的出现与"nothing to pay"
不再误报；`tests/test_settle_dry_run.py`、`tests/test_what_if_records.py` 里钉住旧句子的断言随句子一起改
（边界 190）。全套 1920 项通过，`pyflakes` 干净。


### 3.142 一个问题问三遍，不等于问三个问题：按顺序的计划

到第 135 轮，页面能回答"把这个文件付掉会怎样"，而且分三族答：接手（`--settle-restore-file`）、丢掉付不掉的对
（`--settle-drop-file`）、重开（`--settle-reaffirm-file`）。三段的账都是真的，加起来却不是一个计划——三条命令
把债在两张账之间搬来搬去：

- 丢掉一条决定，不是解决它，是把"重开付不掉"变成"没人接手"（边界 318）；
- 接手一条决定，又把"没人接手"变回一条站着的决定；
- 重开只改"这条决定现在从哪一刻说话"，不动上面两个账本。

所以同一批命令换个顺序，最后的账不一样。读的人把三段抄下来自己排顺序，排出来的结果没人预言过。

第 136 轮加的是顺序本身：`--campaign --what-if-records FILE --plan restore,drop,retake`，每一步都在 store 的
**拷贝**上跑一条真命令（`preview_settlement_change` / `preview_withdrawal_change`），每步之后重算两张账，
最后再按相反顺序算一遍，只有在结果不同的时候才说"顺序有影响"：

```text
$ brian evolve --campaign --what-if-records data/smd/stream-two.json --plan restore,drop,retake
what if          data/smd/stream-two.json owes 4 unanswered withdrawal(s), 1 stale decision(s) and 0 decision(s) no
                 re-take can pay
                 in the order: restore, drop, retake
                   after restore  4 pair(s) -> 0 unanswered withdrawal(s), 1 stale decision(s), 2 cannot be re-taken
                   after drop     2 pair(s) -> 2 unanswered withdrawal(s), 1 stale decision(s), 0 cannot be re-taken
                   after retake   1 pair(s) -> 2 unanswered withdrawal(s), 0 stale decision(s), 0 cannot be re-taken
                 the order matters: done the other way round it would end at 0 unanswered withdrawal(s), 0 stale
                 decision(s) and 2 it cannot re-take (boundary 322)
                 nothing here is written: every step runs on a copy of the store, so the order is the only thing
                 that changed (boundary 321)
```

反过来问一遍，现场就露出真正的发现：

```text
$ brian evolve --campaign --what-if-records data/smd/stream-two.json --plan retake,drop,restore
                 in the order: retake, drop, restore
                   after retake   1 pair(s) -> 4 unanswered withdrawal(s), 0 stale decision(s), 0 cannot be re-taken
                   after drop     0 pair(s) -> 4 unanswered withdrawal(s), 0 stale decision(s), 0 cannot be re-taken
                   after restore  4 pair(s) -> 0 unanswered withdrawal(s), 0 stale decision(s), 2 cannot be re-taken
```

**接手一条"记录已经不认那个后继"的决定，会立刻生产出 2 条"重开付不掉"的对。** `--settle-restore` 只检查这对
决定存在，没检查那条记录现在还认不认这个后继；`reaffirm_link` 在同样的情况下是拒绝的（第 119 轮）。于是同一条
约束在两个写入口上不一样严：重开拒绝断言账本否认的事，接手却照单全收，然后由下一个人去收拾。计划功能本身没有
制造这个缺陷，它只是第一次把两条命令并排放到同一张纸上看。

**边界 321：三条命令各自"付完之后还剩什么"，不等于"按这个顺序做完还剩什么"；答案里必须有时序，
或者说清它只是单步的账。**

**边界 322：顺序会影响结果时，页面必须自己算一遍反序并说出来，否则读者得到的是他恰好抄下来的那个顺序。**

**边界 323：同一条约束不能只在一条写入口上校——重开拒绝的事，接手也不能默认接受；
一个计划能把两个入口并排照出来，但照出来之后得有人补上另一边。**

测试：`tests/test_what_if_plan.py`（7 项）覆盖每步的账、顺序有影响时的提示、单步不假装有顺序、
计划不写盘（store 与两张账逐字不变）、`--plan` 没有那个问题时被拒绝、未知步骤被点名、以及页面数字与
`_plan_counts()` 一致。全套 1929 项通过，`pyflakes` 干净。


### 3.143 一个约束要在两扇门上都检查：恢复与重开的两道门

第 136 轮按顺序跑计划时，反向顺序给出的结果里有一行很刺眼：`after restore -> 2 cannot be re-taken`。也就是说，只要先恢复再重开，就会凭空多出两件"没有任何重开命令能付"的活；而第 133 轮的成果正是把这类活从可付清单里剔掉。往回追，原因不在计划，在写入端：

- `reaffirm_link`（第 119 轮）一直拒绝在一份"已经不指向这个后继"的记录上重开决定，理由是那样等于断言账本否认的事；
- `restore_settlement`（第 126 轮）做的是同一类断言，却从来没有这一句检查。

同一个约束，两道写入的门，只检查了一道。于是"把撤回的决定拿回来"可以造出一件它自己付不掉的活，而页面（第 133 轮起只数可付的）会在下一轮把它如实算成"付不掉"，读者看到的是自己的债凭空长了两笔。

这一轮把检查补到第二道门上，并把这道门的两半拆开：

```
refused: the record no longer names round-tenth.json as its successor (it names round-elsewhere.json):
         taking the decision up again would assert something the ledger denies.
         Record a new decision with `--settle-link round-ninth.json=<a successor the record names>`,
         or drop it with `--settle-drop round-ninth.json=round-tenth.json` (boundary 323)
```

拒绝语同时给两条出路，因为这里确实有两条：改成一个记录实际指向的后继（新决定），或者干脆撤掉（转成"欠一个决定"，它有自己的队列、年龄和按文件分组）。

**撤回队列也随之一分为二。** 第 128 轮把"没人接手的撤回"按文件分组，前提是每一笔都能被 `--settle-restore` 付掉；第二道门一收紧，这个前提就不成立了。于是 `withdrawal_plan` 的每一行现在有两个数：

```
links             1 withdrawal(s) on data/smd/stream-two.json cannot be taken up again: the records name
                  other successors -- brian evolve --settle-drop-file data/smd/stream-two.json --settle-why TEXT
links             withdrawn and waiting, by stream file: data/smd/stream-two.json 2 decision(s); data/smd/stream-real.json 1 decision(s)
```

第一行是真的付不掉的那一半，第二行是总数（`waiting`，不是 `decisions`）——两行一起才对得上"这个文件还欠多少、其中多少能付"。按文件那条命令也照此行事：

```
leaving        1 withdrawal(s) no restore can pay: the records name other successors -- `brian evolve --settle-drop-file data/smd/stream-two.json --settle-why TEXT`
re-taking       1 decision(s) on data/smd/stream-two.json in one command
taken up again  round-sixth.json -> round-seventh.json
```

它先把能付的付掉，再把付不掉的说出来并给出下一条命令；付不掉是新常态而不是错误，所以退出码仍是 0。反过来，当一个文件只剩付不掉的那一半时，它不再假装"没有欠账"，而是：

```
1 withdrawal(s) on data/smd/stream-two.json cannot be taken up again: the records name other successors --
`brian evolve --settle-drop-file data/smd/stream-two.json --settle-why TEXT`
```

（退出码 2：这件事需要人做决定，没有一条 restore 能推进它。）

**"如果全付掉"这个问题也补齐了这一半。** 第 132 轮的问题问的是"付这个文件会剩什么"，第 133 轮之后它只算可付的部分，于是答案是"剩 0"而读者会以为"什么都不欠"。现在它先说总数，再点名那半边够不着的：

```
what if          data/smd/stream-two.json owes 2 unanswered withdrawal(s), 1 stale decision(s) and 2 decision(s) no re-take can pay
                 paying the withdrawals (1 pair(s), `--settle-restore-file data/smd/stream-two.json`):
                   after this run  data/smd/stream-two.json would owe 0 unanswered withdrawal(s), and the ledger would go from 2 to 1
                 NONE of that reaches 1 withdrawal(s) here: the records name other successors, so no restore can take
                 them up again -- `brian evolve --settle-drop-file data/smd/stream-two.json --settle-why TEXT`
```

这三处输出都由同一份 `withdrawal_plan` 生成（第 131 轮的规矩：预言和结果共用一次计算），所以页面、干跑、命令不会各说一套。

**边界 324：** 同一个约束有两道写入的门时，必须在两扇门上都检查；只在一扇上检查，等于把那扇门当成建议。计划页能把两道门并排放在读者眼前（这一轮正是这样发现的），但发现之后仍要去改另一扇门——否则计划页只是把别人的 bug 印得更清楚。

**边界 325：** 一条队列里若有"没有任何命令能付"的一半，页面必须分别说出这一半和总数，而且两个数都要来自同一份计划，不能一处用可付数、一处用总数。总数小了读者会以为债在减少，总数大了读者会去找一条不存在的命令。

**边界 326：** 拒绝时必须把"下一步能跑的命令"一起说出来。这里是"记录已经指向别的后继"，那么给出 `--settle-link`（朝着账本实际说的方向重记）和 `--settle-drop`（转成欠一个决定）两条路；只说"不行"等于让读者再猜一次，而猜测的成本正是这一整层记账要省掉的东西。

这一轮没有改动任何测量：`--settle-restore` 的语义仍是"把撤回的决定重新立起来"，只是它现在只对账本还认的那一对生效；`--audit --strict` 在补检查之前是 0、补完之后对同一份账本仍是 0（demo 里恢复过的对子不再计费），而 `--max-withdrawn-per-file 0` 在同一份账本上退出 1，点名那一个文件——两种闸门各自回答自己的问题。


### 3.144 目标搬家了，裁定没有：重新锚定，以及"两扇门都拒绝"之后的那一步

第 133 轮让清单不再把"记录已经不指的那一对"算成待付（因为 `reaffirm_link` 会拒绝）；第 137 轮让 `--settle-restore`
用同一条约束拒绝。两处拒绝都是对的，合起来却把这条裁定逼进了死角：

- 清单说"这一对不可付"，于是没有队列会把它报价；
- 恢复门说"记录已经不指它了"，于是 `--settle-restore` 拒；
- 只剩两条路：**记一条新裁定**（旧的那条永远留在"不可付"里），或者 **`--settle-drop`**（把这条裁定扔掉）。

两条都不回答拒绝本身提出的问题：**记录搬家了，裁定没有。**

第 138 轮补的是第三步：

```
$ brian evolve --settle-reanchor round-other.json \
      --settle-why "the record was retargeted to round-elsewhere.json" --settle-by carol
re-anchored    round-other.json  round-new.json -> round-elsewhere.json
                the decision now names what the record names, and the earlier target stays in successor_history
                because: the record was retargeted to round-elsewhere.json, and the decision still names round-new.json
                re-anchored by: carol
                a re-anchored decision was not taken on the new successor: the page says so, and only a check can
                measure it there (boundary 203)
repair plan      nothing left the table: cost 0 -> 0, 0 file(s) still owing
```

要点有三条：

1. **只写 store，不写账本。** 改瞄的是 `settlements.json` 里那一条裁定的 `successor`，记录文件、`ledger.jsonl`
   一个字节都不动——裁定是"人读过账本之后的判断"，让它去改账本就是让判断改证据（边界 329）。真要修记录的字段，
   那是 `--set-stream-path` 那一类命令的活（第 93–96 轮），两者的留下痕迹的方式不同。
2. **旧目标留在 `successor_history`。** `at` 不动，改动另起一条 `{"from","to","at","why","by"}`。页面上因此能说
   "这条裁定是在 `round-new.json` 上做的，后来改锚到 `round-elsewhere.json`"——这正是第 203 轮给"修过的路径"
   定的规矩，挪到"修过的目标"上一个字段。`--history` 在那一行标
   `[re-anchored on 2026-11-20T06:23:23+00:00: round-new.json -> round-elsewhere.json]`，紧挨着同一行的
   `[retired, re-measured as round-elsewhere.json]`，两件事并排读得出来。
3. **改锚不等于付掉。** 改完之后这一对**不在清单里了**，因为它不再是"账本动过、裁定没动"的状态——记录自己的退休标记
   就是裁定的前提，不是与它竞争的第三份主张。只有当**又一份**晚于改锚时刻的主张出现，它才重新变成"待重开"，
   而那时它是**可付**的：

```
$ brian evolve --settle-reaffirm-file data/smd/stream-one.json --settle-why "a later claim arrived" --settle-by alice
reaffirmed     round-other.json -> round-elsewhere.json
```

实测（`var/demo138`，从 `var/demo137` 复制）：改锚前清单写
`2 stale decision(s) on data/smd/stream-two.json cannot be re-taken at all: round-other.json -> round-new.json
(the record now names round-elsewhere.json) -- brian evolve --settle-drop round-other.json=round-new.json`；
改锚后这一行变成 `1 stale …`（只剩另一条），并多出

```
links             1 decision(s) were re-anchored to the successor the record now names: round-other.json round-new.json -> round-elsewhere.json
links             a re-anchored decision was not taken on its new successor: only a check measures it there, and `--history` marks the rows
```

`--campaign --json-out` 里那条裁定的 `successor_history` 是
`[{"from": "round-new.json", "to": "round-elsewhere.json", "at": "…", "why": "…", "by": "carol"}]`，
`settlement_summary["reanchored"] == 1`；`--audit --strict` 仍旧 0（改锚不产生新的收费理由）。

这一轮的实现缺陷记两条：其一，CLI 分支里写了 `import json as _json`，让它成了 `cmd_evolve` 的局部名，
函数里其它分支一读就是 `UnboundLocalError: cannot access local variable '_json'`——模块级的 `json` 已经在，别在分支里再导一次；
其二，campaign 那半截脚本在 CLI 那半截的断言处先中断，于是**页面一句话都没写**，测试红了两条才看出来。
一条"三种页面都要说"的改动，必须逐个页面确认输出，而不是相信脚本跑完了。

**边界 327：一条裁定的目标搬了家，不等于这条裁定要重做。** 它可以是"同一条裁定，换一个目标，留下痕迹"，而页面必须同时
印出两个目标——否则一次改锚读起来就像那条裁定一直是在新目标上做的（边界 203 的同一个错误，换个字段）。

**边界 328：拒绝必须留下下一步。** 清单拒付、恢复门拒签，两条都对；但如果没有任何一步能消化这对矛盾，读者实际只有
"重开一条"和"扔掉"两个选择，而那正是让一条裁定变成永久噪声的办法。两扇门都关上时，第三扇门是这个层级的义务。

**边界 329：裁定可以改自己的目标，不能改它的证据。** 改锚写 store，账本与记录一个字节不动；如果某天需要改记录本身的
字段，那是另一类命令（第 93 轮），"判断"与"证据"必须各走各的门，否则一条裁定就能改写它自己被检验的那份材料。


### 3.145 一条命令付一整个文件的"目标搬家"：第四族也要进预览

第 138 轮给了单条裁定"跟着记录走"的那一步。可是被改过目标的文件不是欠一步，是欠 N 步，而手打 N 次正是漏掉一次的办法——
这正是第 128/129/137 轮让"按文件"成为命令形态的理由。第 139 轮把这条腿补齐，并且顺手把"什么是可付的"问到了第四族：

```
def reanchor_plan(rounds, settlements) -> List[Dict[str, Any]]:
    """仍然指向记录已经不指的那一支的裁定，按记录的文件分组。"""
    for item in active_settlements(settlements):          # 只有"还站着"的裁定欠这一步
        named = _named_successor(by_name.get(record))
        if not named or named == item["successor"]:       # 记录没有了目标，或者两边已经一致
            continue
        bucket = buckets.setdefault(origin, {"origin": origin, "records": [], "pairs": [], "batch": ""})
```

三条约束落在这里：

1. **清单先于命令。** `--settle-reanchor-file STREAM` 的记录不从参数里来，从 `reanchor_plan` 来——页面印哪几行，命令就付哪几行
   （第 101 轮的规矩）；它随后递归进单条命令那一段，于是拒绝语、`repair plan` 行、JSON 载荷都只有一份实现（边界 284）。
2. **一次理由，一条命令。** `--settle-reanchor` 改成 `nargs="+"`，一条 `--settle-why` 覆盖整批（第 277/303/316 轮同一个形状）；
   被拒的一条记 `refused` 并继续下一条，整批退出 2；JSON 载荷从字典改成**列表**加一个 `reanchor_refused` 计数（边界 278）。
3. **页面没有第四族就还是旧的答案。** `--campaign --what-if-records` 曾经问三族：撤回、不可付（删掉）、重开。改锚改变"可付"的构成
   却一条都不付，所以预览必须同时印出两个数——这一批做完还能付多少、还有多少仍指向旧目标：

```
$ brian evolve --settle-reanchor-file data/smd/stream-two.json \
      --settle-why "the record was retargeted to round-seventh.json" --settle-by carol --dry-run
would re-anchor  1 decision(s) on data/smd/stream-two.json in one dry run: nothing was recorded
  round-sixth.json  (round-new.json -> round-seventh.json)
nothing was written; drop --dry-run to re-anchor them (boundary 304).
after this run  data/smd/stream-two.json would owe 0 decision(s) no re-take can pay, and 0 decision(s) would still
                name a successor their record has left (the ledger goes from 1 to 0)
                a re-anchored decision was not taken on its new successor: the page says so, and only a check
                measures it there (boundary 203)
```

实测（`var/demo138`，第 138 轮的演示根）：改锚前 `--campaign` 只印一行指路

```
links             1 decision(s) still name a successor their record has left: round-sixth.json round-new.json -> round-seventh.json -- brian evolve --settle-reanchor-file data/smd/stream-two.json --settle-why TEXT --settle-by NAME
```

真跑之后那一行消失，换成 `2 decision(s) were re-anchored to the successor the record now names: round-other.json
round-new.json -> round-elsewhere.json; round-sixth.json round-new.json -> round-seventh.json`；`--history` 第 7 行同时挂着
`[retired, re-measured as round-seventh.json]`、`[settled with round-seventh.json by a recorded decision]`、
`[re-anchored on 2026-11-20T06:41:09+00:00: round-new.json -> round-seventh.json]` 三条标记；`--campaign --json-out` 里
`reanchorable` 变成 `[]`，那条裁定的 `successor_history` 长度 1。

顺带记一条**静默**：文件级命令的 `--json-out` 什么都不写（`inner.json_out = None`，与 `--settle-drop-file`、`--settle-restore-file`
一致）。批量没有唯一的一条可命名的记录，所以载荷留给单条命令——这个决定本身没错，但一个看起来会产出文件的标志静默地不产出，
就是"装饰性标志"的同一个毛病（边界 305），所以它进了 §6 第 126 项。

**边界 330：改锚改变"可付"的构成，却一条都不付，所以它的预览必须印两个数。** 一个是"还能付多少"，一个是"还有多少仍指向旧目标"；
只印前者，读者会把"欠账换了形态"读成"欠账少了一半"，而后者才是这一族真正在移动的账。

**边界 331："我把这些全付了会怎样"必须问能改变答案的每一族。** 家族是会长的：第 132 轮问两族、第 135 轮补到三族、这一轮补到四族。
每补一族，之前那句"没有可付的了"就多一次可能是错的——所以它必须由四族共同决定，而不是"我认识的那几族"。

**边界 332：文件级命令的记录只能来自页面印出来的那份清单。** 命令付的东西若不在读者刚看过的那几行里，这一条命令就是一次
没有预告的写入；反过来，页面印了却没人付，那行就是一条永远发不出的催款单（第 261 轮的同一个错误）。


### 3.146 同一个开关，四种回答：批量到底写不写载荷

第 126 条要的是一件小事：四个"一条命令付一整个文件"的命令，对 `--json-out` 说同一句话。查下来它们说了四种话——
`--settle-reaffirm-file` 与 `--settle-restore-file` 把 `args.json_out` 原样递归进去，单条那段写一个列表；而
`--settle-drop-file` 与 `--settle-reanchor-file` 在递归前把 `inner.json_out = None`，于是这个开关**什么都不写，也不说**。
空的标志就是装饰（边界 305 的原话），只不过这次空在另一头：不是"没这道闸门"，是"闸门在，门后没有东西"。

两条路里选了"写整批"而不是"印一句'批量不写载荷'"。理由不是省事：单条那一段本来就支持多条（`--settle-reanchor`
第 139 轮起是 `nargs="+"`，`done` 是累加的），所以批量要的载荷在它手里已经齐了，把 `json_out` 传进去就是整批的答案——
一条命令、一份实现（边界 284/304 的同一条理由）。选"印一句不写"，等于给同一族留一条只好不用的分支。

```
$ python -m brian.cli evolve --settle-reanchor-file data/smd/stream-two.json --settle-why "the record was retargeted" \
      --settle-by carol --json-out p140.json --dry-run
would re-anchor  2 decision(s) on data/smd/stream-two.json in one dry run: nothing was recorded
  round-other.json  (round-new.json -> round-elsewhere.json)
  round-fifth.json  (round-new.json -> round-elsewhere.json)
nothing was written; drop --dry-run to re-anchor them (boundary 304).
no payload      a preview writes nothing, not even the file --json-out names (boundary 304).

$ ... 去掉 --dry-run，同一份载荷就在 p140.json 里：
{"reanchored": [{"record": "round-other.json", "from": "round-new.json", "to": "round-elsewhere.json", ...},
                {"record": "round-fifth.json", "from": "round-new.json", "to": "round-elsewhere.json", ...}],
 "reanchor_refused": 0}
```

`--settle-drop-file` 现在写 `{"withdrawn": [...], "drop_refused": 0, "drop_skipped": 0}`，两条都在里面。四个命令因此
是同一个形状：`{"<族名>": [...], "<族名>_refused": N}`，列表在前——照着写脚本的人不必为哪个文件命令特判。

预览那一侧补了一行：四条 dry-run 分支在"nothing was written"之后印
`no payload      a preview writes nothing, not even the file --json-out names (boundary 304).`。不印这一句，
`--json-out` 在预览下就是被忽略；而"被忽略"与"做了但结果为空"在读者那里长得一模一样。

最后一处是拒绝：一条都没付的批量（清单说这个文件没有欠账）退出 2，**不写**载荷。空载荷会被读成"跑过了，谁也没付"，
而真相是"这次运行没有可付的东西"——两种事实不该共用一个文件（边界 155 的老问题换了个位置）。

**边界 333：同一族的命令对同一个开关只能有一种回答。** 四种回答里只要有一种是"静默"，读者就只能靠猜，而猜错的
代价是他以为写好了。

**边界 334：预览不写任何东西，包括它自己的载荷；所以预览必须明说这一点。** "没写"是行为，"说了没写"是承诺。

**边界 335：拒绝不是付款。** 一次什么都没付的批量不写载荷——空的列表会被读成"付过，付了零个"。


### 3.147 载荷也要报账：八处写文件，一种写法

第 127 条说"同一个标志在两条线上有两种可见性"。查下去比这句更糟：整个 CLI 里有**八处**写 `--json-out` 载荷，
八处八样——六处不吭声（`--settle-link` 的两处载荷、`--settle-note`、`--settle-reanchor`、`--settle-drop`、`--settle-restore`、
`--settle-reaffirm`、`--repair-links`），`--repair-links` 那处甚至用 `__import__("json")` 绕开已经导入的模块；一处自己
建父目录（写轮次记录那处，第 113 轮），其余的父目录不存在就直接抛；而**写不出来时抛的是回溯**。最后这条最贵：

```
$ python -m brian.cli evolve --settle-reanchor-file data/smd/stream-two.json --settle-why x --settle-by carol \
      --json-out nodir\p.json
  File ".../src/brian/cli.py", line 3538, in cmd_evolve
    with open(out, "w", encoding="utf-8", newline="\n") as handle:
FileNotFoundError: [Errno 2] No such file or directory: 'nodir\\p.json'
```

而那时**裁定已经写进 store**：两条决策已经改锚。所以读者拿到的是一次回溯、一个非零退出码、和一个已经改了账的本子——
三者互相矛盾，而唯一能让它们一致的东西（那句话"已经记下了，只缺这个文件"）没有印出来。

改法是把这件事收到一处：`_write_payload(path, payload) -> bool`。建父目录（第 113 轮的规则推广到所有载荷）→ 写
`<target>.tmp` → `replace` 落位（半截的载荷不会被当成完整的）→ 印 `wrote <path>`，与非裁定命令一直用的措辞一致。
写失败时印两行——

```
cannot write the payload to .: [WinError 5] ...
                what this command recorded is already in the store: only the payload is missing
```

——并且调用方退 2。九处替换（八个旧点 + 新增的 `--repair-links`）之后，全族一个形状。

```
$ ... --json-out deep\nested\p141.json        # 父目录不存在
re-anchored    2 of 2 decision(s), 0 needed no change, 0 refused
wrote deep\nested\p141.json
$ ... --json-out .                             # 目标是目录
cannot write the payload to .: [WinError 5] ...
                what this command recorded is already in the store: only the payload is missing
$ echo $LASTEXITCODE
2
```

一处自己踩的坑：第一版写的是 `temp = target.with_name(target.name + ".tmp")`，而 `Path(".").name` 是空串，
于是 `with_name` 抛的是 **`ValueError` 而不是 `OSError`**，回溯照旧。改成 `Path(str(target) + ".tmp")` 并把 `ValueError`
一起接住。"接住 `OSError`"不等于接住所有失败的写法——这本身就是下面 337 的一个例子。

**边界 336：一个文件写没写，页面上必须能看得见。** 写了的印 `wrote <path>`，没写的不能有这一行；否则这句话不再
意味着任何事（同一句话在八个地方被念，就等于没有念）。

**边界 337：载荷是衍生物，不是证据。** 它写不出来时，命令必须说"记录已经进库、只缺这个文件"并非零退出；用回溯把
一次已经付过的运行变成一次"不知道结果"的运行，是把读者最需要的那条信息扔掉了。

**边界 338：一个动作在多处重复实现时，一致性只能靠收到一处，不能靠每一处都记得。** 八个写载荷的点里，一个建目录、
一个绕过 `json`、六个不吭声；没有人写错，只是没有人把它当成同一件事。


### 3.148 走一圈的手，要交回那一圈的成绩单

第 141 轮把八处写载荷的地方收进 `_write_payload` 之后，`--json-out` 只剩下一个静默点，而且偏偏是这层里花钱最多的那个命令：`--verify --from-queue N`。

它遍历队列，对每个记录调一次内层的 `--verify`；内层必须把 `json_out` 置空——一个记录写一个文件，第二个会把第一个覆盖掉（第 140 轮已经踩过一次：两个族共用一个根目录时，读回来的还是旧载荷）。置空是对的，错的是**置空之后什么都没交回来**：一整趟队列跑完，测了哪几个版本、各拿到什么退出码、哪一条被账本拒了，全都只在 `var/evolve/queue_runs.json` 里，读者得自己去翻那个文件。一个花掉 N 次测量的命令，产出不该只留在自己的日志里。

现在的形状是：**一整趟队列，一份载荷，内容就是这趟刚写进仓库的那条记录。**

```
wrote walk.json
1 promise(s) measured on the file they were born with. A copied end is measured, never applied; a contradiction is still refused.
```

```json
{
  "walks": [
    {
      "at": "2026-11-20T07:09:27.207515+00:00",
      "limit": 1, "runnable": 1, "measured": 1, "skipped": 0,
      "skipped_origins": [], "cleared": 0, "worst": 0, "stopped_at": 0,
      "attempted": [{"version": 20, "code": 0, "why": ""}]
    }
  ]
}
```

`attempted` 里每个版本带着它的退出码和拒绝理由，`stopped_at` 说明这趟是不是撞在矛盾上停下——和 `--campaign` 读的是同一条记录，不是给这个旗标另写一份摘要。**交出去的必须是仓库留下的那条**，否则两处数字迟早会分开。

三件事跟着这一改：

1. **空的一趟也是一趟**。队列里全是"出生文件不在这个检出里"的记录时，它照样写一条 `runnable: 0, measured: 0, attempted: []` 的记录（第 103 轮的理由：没人测过的东西，也要有"没人去过"的观测），于是也一样交回这份载荷。差别只在数字上，不在形状上。
2. **计划不交东西，并且说出来**。`--from-queue 0` 只列出要跑哪些记录、一个都不测，也不写仓库记录——那就没有"这一趟"可以交。此时若给了 `--json-out`，页面打印

```
no payload      a plan measures nothing, so there is no walk to hand over (boundary 339).
```

空数组会是一句谎话：它说的不是"这趟没写记录"，而是"仓库里没有记录"。所以这里不写文件、只说一句话。这和第 140 轮预览分支的那句话是同一条规矩，用在了第三种"没有产出"上。

3. **载荷写不出来，退出码要变**。第 141 轮的 `_write_payload` 返回 False 时，遍历把 `worst = max(worst, 2)`——测量已经发生、账本已经改过，那么失败的就只是载荷，退出码说的必须正是这一件事（边界 337 的第二处应用）。

于是 `--json-out` 在这层里不再有静默点：一次命令要么交出 `wrote <path>`，要么交出一句"为什么没有东西可交"，要么交出一句"写不了，记录已经在仓库里"。

**边界 339：一个什么都没记录的命令，不能假装交了空的东西；它要么说清"没有这一趟"，要么说清"这一趟测了零个"。**

**边界 340：批量写入的载荷必须是整批的一份，而且必须就是仓库留下的那条记录；另写一份摘要，等于给同一件事准备第二个说法。**

**边界 341：在循环里把旗标置空，只有在循环外有人替它交回等价的东西时才合法；否则那是一次静默的丢弃。**

### 3.149 一次行走要有序号：页面说"最后一次"，就得说清是第几次

第 142 轮让 `--from-queue` 交回它写进仓库的那条记录，`--campaign` 也早就把每一次行走都汇总成一句话。三处说的是同一件事——`var/evolve/queue_runs.json` 里的一条记录——但**三处都没有说清是哪一条**。一份载荷在你手里，页面在屏幕上，仓库在文件里，要把它们对上，只能拿时间戳一行行肉眼比对；而时间戳是这一层里最容易撞车的东西（第 73 轮就在同一秒里写坏过两个记录）。

所以这一轮把"位置"变成标签：**序号就是仓库里的位置**。

页面：

```
links             the last queue run was 2026-11-20T07:16:28.355713+00:00: measured 0 of 0 runnable
                  promise(s) on their origins, left 0 out (walk 3 of 3)
```

载荷：

```json
{
  "store": {"path": "var/evolve/queue_runs.json", "ordinal": 3, "of": 3},
  "walks": [{"at": "2026-11-20T07:16:28.355713+00:00", "limit": 1, "runnable": 0, "measured": 0,
             "skipped": 0, "cleared": 0, "worst": 0, "stopped_at": 0, "attempted": []}]
}
```

JSON 的另一半是 `--campaign --json-out` 里的 `walks` 块，整份仓库按原顺序交出去，每条带上 `ordinal`：

```
[ (1, 1), (2, 1), (3, 0) ]      # (ordinal, measured)
```

页面只印最后一条（读者要动手的就是那条），但这次它印出 `(walk 3 of 3)`——**"最后一次"不是一个坐标，"第 3 次，共 3 次"才是**。这一句和第 71 轮那条"债务要按域拆开"是同一种毛病：总量看起来健康，拆开才知道在哪。

三处细节：

1. **序号从仓库读，不另记**。`_walk_store` 在记录写完之后回头读 `queue_runs.json`，用 `at` 找这条记录的下标；`campaign()` 也是遍历仓库加序号。要是另有一个计数器，它就只是"仓库里有多少次行走"的第二个说法，而两个说法迟早会分开（第 83 轮"修补自报"、第 140 轮"两份载荷"都是这个形状）。
2. **空的一趟也有位置**。上面那份载荷就是 `runnable: 0` 的一趟：它照样写了一条记录，于是照样有 `ordinal`。第 142 轮说"空的一趟也是一趟"，这一轮补上"于是它也排得上号"。
3. **页面不替代清单**。`(walk 3 of 3)` 说的是"还有两次你看不见"；要那两次，读 `walks` 块或打开仓库。一句"最后一次"会让人以为那是全部——这正是这一层反复修的那类错觉。

**边界 342：页面和载荷各描述仓库里的一条记录时，必须按"在仓库里的位置"称呼它，而不是按一个要读者肉眼比对的时刻。**

**边界 343：序号只能从仓库读出来，不能另立一个计数器；同一件事有第二个记账处，就迟早会有第二个答案（第 83/140 轮）。**

**边界 344：页面只印 N 条里的最后一条时，必须说明 N；"最后一次"不是坐标。**

## 3.150 两本账簿：重跑队列也留住自己的行走

第 130 条是上一轮写下的，它假定只有一个仓库。实际有两个，而且两个队列付的钱不是一种钱：

- `--from-queue` 走的是**承诺**：它去原始文件上重新量一遍已发布版本，写下 `verification`，不产生任何一轮。
  它的行走记在 `var/evolve/queue_runs.json`（第 103 轮），第 143 轮又给它加了序号。
- `--rerun-unrecorded` / `--with-ceilings` 走的是**轮**：它把没人认领的旧轮重新量一遍、退休、写一个继承者。
  它付的每一轮里都有一个 `queue` 块（第 58/78 轮），而**它自己没有任何行走记录**——所以第 130 条要求的
  "记下这是第几次行走"，今天没有可指的仓库。

于是一轮说"是 file 队列付的"，读者却无法回答"哪一次"。修复分四步：

1. `src/brian/promote.py` 新增 `var/evolve/rerun_runs.json`（`brian.rerun/1`、上限 20 条、`trimmed` 计数）。
2. `begin_rerun_run(root, run)` **在第一次测量之前**占位，返回 `{"walk": n, "of": m}`。占位必须早于花钱：
   轮是在测量过程中写的，如果行走只在结束时记，那么它付的每一轮都会指向一个当时并不存在的序号。占位
   也顺带说明了无人看管的情况——中途死掉的命令留下一条 `finished: ""` 的记录，说的是"这次行走开始过、
   没有结束"，而不是什么也没有。
3. `finish_rerun_run(root, walk, run)` 回来**改写同一条**记录。序号是身份，所以收尾不能再追加一条：那会把
   序号变回时间戳（第 142 轮才刚把时间戳从页面上拿掉）。若中途被裁剪，它返回 `None`，页面照实说"它付的
   轮指的那条记录已经不在了"。
4. `_queue_entry(..., walk=n)` 把序号写进轮里的 `queue.walk`；`--history` 的行从
   `[paid by the file queue]` 变成 `[paid by the file queue, walk 1]`；`_paid_round_names(root, walk)` 从记录
   里读回这个名字（不在循环里另攒一份——那样一个写了记录却没退休成功的轮会被两份账各算一次）。

**没有活的时候不占位。** 第一次实现是无条件占位，于是空队列也留下一条 `paid: 0` 的记录，页面还打印
`walk 1 of …`。这会让序号的含义变成"有人在这队列还没活的时候敲了十二次命令"。现在只有
`ready[:limit]` 或 `ceiling_ready[:ceiling_limit]` 非空才占位：

```
brian evolve --rerun-unrecorded --rerun-limit 1     # 队列为空
families          0: 0 round(s) from the file queue
                                                    # 没有 walk 行，也没有 rerun_runs.json
```

有活的时候（`var/demo144`，从 `var/demo127` 造出来的一轮被抹掉 `stream_path` 并补回候选定义）：

```
walk              1 of the re-run queue's recorded invocation(s); each round it pays names it
---- re-running round-old.json on data/smd/stream-one.json
```

测试里把 `measure` 换成固定值，走的是同一条 CLI 路径，因此能看到钱真的付出去之后的形状：新轮的
`queue["walk"] == 1`，而 `rerun_runs.json` 的第 1 条是 `{"paid": 1, "rounds": ["round-…"]}`。

同一条路径也真的走了一遍（`var/demo144`，Laya 在 `127.0.0.1:8077`，`reasoner offline`）：`recorded round
var\evolve\round-20261120T073000Z.json` 之后，那轮的 `queue` 块是

```
{"paid": "SMD", "paid_round": "round-old.json", "family": "file", "walk": 1, …}
```

而 `--history` 的两行正好把两种记录并排放着——旧的一轮只有家族，新的一轮说到行走：

```
 2  …  [paid by the file queue]  [replacing round-old.json]  …
 9  …  [paid by the file queue, walk 1]  [replacing round-old.json]
```

**边界 345：一条记录声称"是谁付的"，这个声称就必须在一个"付之前就写下的仓库"里能对上。**
事后补写的序号不是证据，它只是一句和记录同源的断言。

**边界 346：没有活的行走不算行走。** 只在有钱可花时占位；否则序号数的不是工作量，是敲键盘的次数。

**边界 347：收尾要改它占的那条，不要追加第二条。** 追加会让序号在完成的那一刻失效——同一件事有两条
记录，而只有一条是轮里指的那个。


## 3.151 走过几次，没人记过：重跑队列的流水，以及被裁掉的那个号

第 144 轮给重跑队列（`--rerun-unrecorded` / `--with-ceilings`）补了一本账簿：每次调用先占一个号，每付掉的轮
记下这个号。第 131 条问的是下一件事——**谁读这本账簿？**当时没有任何一页读过它，于是两种完全不同的状态在
所有页面上长得一模一样：队列走过一次，和走过二十次；一次付掉五轮的行走，和一次被所有轮拒绝的行走。

现在 `--campaign` 有一行：

```
re-run walks      2 invocation(s) of the re-run queue; 1 paid no round; 3 round(s) paid in all; the last was
                  walk 3 at 2026-11-20T07:30:00.387378+00:00
```

"走过几次 / 几次空手而归 / 一共付掉几轮 / 最后一次是几号"——四个数各自回答一个不同的问题，缺一个就会被误读：
只印次数，"20 次"读起来是勤奋；加"几次空手"才知道有多少次是白跑；加"一共付掉几轮"才知道规模；加号才知道
指的是仓库里的哪一条。若仓库裁过，句子再接一句：`; 2 earlier walk(s) were trimmed out of the store, so a
round naming one of them names a record nobody can open`。

**要这个数，就暴露了第 144 轮里那个"号"不是号。** `begin_rerun_run` 返回的是**列表里的位置**（`len(runs)`），
`finish_rerun_run` 也是按 `walk - 1` 取位置；而仓库在 `RERUN_RUN_LIMIT = 20` 条时丢掉最旧的。于是裁一次，
位置整体前移一位，"walk 5" 指的就不再是原来那一条；裁三次，它指向的是别人的行走；再裁，它什么也不指。轮里
记的那个数没变，含义却变了——这比没有号更糟，因为页面上它看起来仍然是个可以查的号。

修法是把这个数**从位置里拿出来**：`ordinal = trimmed + len(runs) + 1`，并且写进条目自己身上
（`{"walk": ordinal, …}`）；收尾按条目自己的号去找，不按位置：

```python
index = next((position for position, item in enumerate(runs)
              if int(item.get("walk") or 0) == int(walk or 0)), -1)
```

于是号在裁剪前后是同一个号。仓库留 20 条，所以更早的轮终究会指向一条**不在仓库里**的行走——这时 `--history`
照旧印它记下的号，但把话说完：

```
[paid by the file queue, walk 1, no longer in the store]
```

不写这一行、直接不印，是另一种谎：读者会把"这轮没说是第几次"当成"这轮没记"，而真相是它记了、记录已经不在了。

**这一轮最不客气的发现来自真账本**：跑 `brian evolve --campaign`，`re-run walks` 那一行根本不出现——因为
`var/evolve/rerun_runs.json` 和 `queue_runs.json` 都不存在。74 份轮记录，**零次队列行走**：这台机器上每一个
付掉的版本，都是人打命令付的。队列从第 100 轮修到今天，账本上它一次也没跑过。页面印不出这一行并不是页面的
缺陷（没有行走就是没有），但它说明"队列让循环跑得动"这句话，在真账本上还没有证据。

所以下一条（第 132 条）不是再给页面加一列，而是**让人手付的那一轮也在仓库里留下一条行走**：`queue.walk = 0`
现在只表示"不是队列付的"，和"没记录"长得一样；把手工那一轮记成一次行走，`--campaign` 才能说"这台机器上的
重跑，队列占到了几成"。

**边界 348：一个数若用来指认一条记录，就不能是它在"会丢旧条目"的列表里的位置。**位置会随裁剪移动，号不能；
把号写进记录自己身上，读数的人才有东西可查。

**边界 349：一本仓库的规模必须出现在页面上。**走过几次、几次空手、一共付掉多少——只印其中一个，另外两个
就会被读者用想象的数字补上。

**边界 350：一个指向不存在的编号，要印成"指向不存在"，不能省略。**省略之后，"记录里没有这个号"和"这个号
已经不在仓库里"变成同一句话，而它们指向两种完全不同的补救。


## 3.152 人打的那一轮也是付款：重跑仓库里的两种行走

第 145 轮的盘点是真账本上 74 份轮记录、**零次队列行走**。这句话有两种听法：一是队列白修了，二是这台机器上的
重跑本来就是人打命令付的，而仓库只记机器付的那些。两种听法指向同一处：`queue.walk = 0` 同时表示"不是队列付的"
和"根本没记"，而页面上"走过几次"数的是机器，读者会把它当成循环干了多少活。

这一轮让**人手那一轮也留下一条行走**，并让仓库自己说清是哪种：

```
retired           round-old.json  (re-measured as round-20261120T073000Z.json)
walk              1, typed by hand: the round it paid was written before this command, so it cannot name the walk
                  (boundary 351)
```

条目多一个 `hand` 字段（队列默认 `false`），序号仍然绝对、仍然写进条目自己身上。于是页面能回答真正的问题：

```
re-run walks      1 invocation(s) of the re-run queue: 0 by the queue, 1 typed by hand; 0 paid no round;
                  1 round(s) paid in all; the last was walk 1 at 2026-11-20T07:46:26.419821+00:00
```

"1 次调用"从此不是一句可读的话，句子必须分成两半：机器走了几次、人走了几次。缺了这半边，`--campaign` 印的
"循环的行走次数"就是"队列的行走次数"，而它被读成前者的机会大得多。

**两条护栏**，都是因为这一轮让"付款"多了一个来源才需要的：

1. **没有后继就不是付款。**`--retire round-old.json`（不写 `--re-measured-as`）是把一个问题关掉，不是付掉它，
   仓库里不留条目。
2. **队列已经付过的不再收一次钱。**若后继轮自己的 `queue.walk` 非零，说明这次付款已经记在仓库里了，于是：
   ```
   walk              already recorded: round-20261120T073000Z.json was paid by the queue's walk 1, so this
                     retirement does not pay for it a second time
   ```
   否则同一笔账记两次，页面上的"付掉几轮"就比真实多一倍。

**这一轮诚实的限制**：人手那一轮在被重跑时就已经写进账本了，这个命令来得更晚，所以它**不能**回头把自己的号
写进那份记录——改写已经发生的记录不是记录，是重写历史。页面因此只说"这次付款记成了第 1 号"，并明说那份轮
记录里没有它的名字（边界 351）。这也是第 133 条的起点：真账本里 74 份记录都诞生在这个仓库之前，因此
`--campaign` 至今只能说"队列走了 0 次、人手走了 0 次"——不是没有人手付过，而是那些付款发生在我们开始记账以前。

**边界 351：付款之后才记下的号，不能被已经写好的那一轮命名。**能做的只有两件事：把号记在仓库里、在页面上说明
这份记录没有它；不能做的是回去改写记录，让历史看起来像当初就记过。

**边界 352：一本仓库若同时装机器的工作和人的工作，就必须分开计数。**合起来的数字不会保持中立，它会被读成机器
那部分，因为那是人们关心的问题。

**边界 353：人付的那一轮也是付款。**只记机器付的，是对"循环做了多少"的谎——不是多说了，是少说了，而少说比
多说更难被发现。


## 3.153 补记的付款：账本早就说了，仓库才开始记

第 146 轮让重跑仓库开始收"人打的那一笔"，但真账本依旧只印 `0 by the queue, 0 typed by hand`。这不是没人付过，
而是那些付款全发生在 144 轮开始记账之前。这一轮把账本里已经写下的退休标记读回仓库：

```
would backfill    round-20261118T020401Z.json -> round-20261118T043911Z.json (the payment is dated 2026-11-18T04:39:39Z)
backfilled        round-20261118T043939Z.json -> walk 17, dated 2026-11-18T04:39:56+00:00
backfilled        17 payment(s) the ledger recorded and the store never saw; each entry says so, and each keeps the
                  moment of the payment (boundary 354)
```

`--backfill-walks` 只读一处：`ledger.jsonl` 里 `retired.re_measured_as`（"这一轮被哪一次重测取代"）。18 份轮记录
带后继，去重后 **17 笔付款**（有一次批量退休用同一个后继替掉了两轮——一笔重测只付一次，按轮记就会说循环付了两遍）。
每条补记的条目带三个字段：`hand: true`、`backfilled: true`、`at` 是**付款发生的那一刻**而不是今天。页面于是能说：

```
re-run walks      17 invocation(s) of the re-run queue: 0 by the queue, 17 typed by hand; 0 paid no round;
                  17 round(s) paid in all; the last was walk 17 at 2026-11-18T04:39:56+00:00;
                  17 of them were written after the fact from the ledger's retirement marks, so they say when the
                  payment happened, not when it was recorded
```

**这一轮最不客气的数字**：补记之后，真账本的 17 笔付款里，**队列付了 0 笔**。第 100 轮到第 146 轮修的整套队列
（`--rerun-unrecorded`、`--with-ceilings`、行走仓库、序号、连"空手而归"的计数）在这台机器的真实账本上一次也没
用过，它只活在测试与演示里。"循环能自己付账"这件事，到今天为止仍然是一个关于代码的性质，不是一个关于这台机器的
事实。这句话必须写下来，因为页面上"17 invocation(s)"很容易被读成"队列跑了 17 次"——这正是分两半印的理由。

**顺手修掉一个第三次出现的缺陷**：`--dry-run` 的守卫里手写着一张家族清单，这一轮加了 `--backfill-walks`，守卫
就把它的预览当成"在没有预览的场合用了 --dry-run"拒掉（第 135 轮第三次出现同一形态：**枚举**才是病，漏掉一项只是
症状）。句子与判断都补上了第五项——但真正的修法（把清单收敛成一张表）留给第 134 条。

**边界 354：事后补记的记录必须自报，并保留事情原本发生的时刻。**记录的年龄和工作的日期是两个事实，只写一个
就会被读成另一个。

**边界 355：把历史读回来记进仓库，不等于当初记下了它。**页面上"17 次"若不带"其中 17 次是事后补记"，读者会以为
这 17 次是循环自己走出来的——那正是这一轮要避免的误读。

**边界 356：一张枚举家族的守卫清单，必须由添加家族的同一次改动更新；更好的做法是让句子和判断都从同一张表生成。**
清单越长，漏掉一项的机会越多，而漏掉的那一项恰恰是新命令——它最需要预览。


## 3.154 一张表，三种读法：家族清单不再是手写的第二份副本

第 135、147 两轮各加了一条批量命令，各自只更新了同一对东西里的一半——要么守卫的判断，要么守卫打印的句子——于是守卫
拒绝了那次改动刚刚造出来的预览。`--dry-run` 的帮助文本是第三份副本，它只点了五条里的两条。这一轮把清单收成一张表：

```python
BATCH_PREVIEWS: Tuple[Tuple[str, str], ...] = (
    ("--settle-reaffirm-file", "settle_reaffirm_file"),
    ("--settle-restore-file", "settle_restore_file"),
    ("--settle-drop-file", "settle_drop_file"),
    ("--settle-reanchor-file", "settle_reanchor_file"),
    ("--backfill-walks", "backfill_walks"),
)
```

判断读它（`previewing_flags(args)`），句子读它（`previews_sentence()`），帮助文本也读它。于是三者不可能再说不同的
话——拒绝与帮助现在逐字给出同一串：

```
  --dry-run             with --settle-reaffirm-file, --settle-restore-file,
                        --settle-drop-file, --settle-reanchor-file or
                        --backfill-walks: print what the batch command would
--dry-run previews --settle-reaffirm-file, --settle-restore-file, --settle-drop-file, --settle-reanchor-file or --backfill-walks; without one of those the command would go ahead and record nothing you could see first
```

**但"有一张表"不等于"这张表被守住"**。真正让下一次改动无法只做一半的，是一条**结构测试**：从解析器里读出
`evolve` 的全部选项，凡以 `--settle-` 开头、以 `-file` 结尾的都在这个家族里，再加上 `--backfill-walks` 这个
命名规则盖不住的例外；两边取合集，必须与表逐项相等。于是新增第六条 `--settle-*-file` 而不进表，测试立刻红。

这条测试必须被证明会触发（边界 359）。`var/prove_148.py` 逐行把表里的那一行删掉，再问测试自己的判据是否还会通过：

```
table == parser family: 5 flag(s), --settle-reaffirm-file, --settle-restore-file, --settle-drop-file, --settle-reanchor-file, --backfill-walks
drop --settle-reaffirm-file   -> the table/parser test fails (caught)
drop --settle-restore-file    -> the table/parser test fails (caught)
drop --settle-drop-file       -> the table/parser test fails (caught)
drop --settle-reanchor-file   -> the table/parser test fails (caught)
drop --backfill-walks         -> the table/parser test fails (caught)
every row is enforced by the structural test
```

五行全部被抓住，包括那个例外——因为例外是钉在测试里的一段显式代码，而不是"大家记得它特殊"。

**边界 357：一个家族只有在它有命名规则的时候才能被机器守住；盖不住的例外必须钉在守它的那条测试里。**
`--backfill-walks` 就是这样的例外，它被显式写进 `offered`，于是它和五条同族一样被强制。

**边界 358：帮助文本、拒绝文本、判断条件是一张表的三种读法。**手写的第二份副本总会朝最伤人的方向漂移——漏掉的
总是最新的那个成员，而它恰恰是唯一还没有被预览过的。

**边界 359：一条"只此一处"的断言，必须被证明会触发；否则它只是一句声明。**`var/prove_148.py` 就是那个证明：
它不是再跑一遍测试，而是把"漏掉一行"这件事真的做一次，看判据是否出声。


## 3.155 0 与 17 之后的那一句：页面要说今天能付几轮、用哪条命令

第 147 轮把账本里的退休标记读回仓库之后，页面一直印着 `0 by the queue, 17 typed by hand`。这句话是真的，却是
**关于过去**的：它说最近这 17 笔付款是怎么记下的，没有一个读者能拿它做决定。第 135 条要的就是补上那一句。真账本
现在这样答：

```
re-run walks      17 invocation(s) of the re-run queue: 0 by the queue, 17 typed by hand; 0 paid no round;
                  17 round(s) paid in all; the last was walk 17 at 2026-11-18T04:39:56+00:00; 17 of them were
                  written after the fact from the ledger's retirement marks, so they say when the payment happened,
                  not when it was recorded
re-run offer      9 round(s) the queue could pay today (9 that named their file but never recorded their ceilings):
                  brian evolve --rerun-unrecorded --with-ceilings --rerun-limit 0; the queue has paid none of this
                  store's 17 payment(s): every one was typed by hand, so the store says nothing about what the
                  queue would do
```

`_rerun_offer()` 每次印页面时重新算：家族来自 `_unrecorded_matters` 与 `_ceiling_matters`（就是守卫与队列用的那两份
名单，所以"能付几轮"不是另一份估计），存入的行走数来自 `rerun_walks()`。三种输出：没有可付的说"今天没有可付的"；
只有未知文件的家族给不带 `--with-ceilings` 的命令；有命了文件却没记上限的家族给带 `--with-ceilings` 的。同一个
offer 也进 `--campaign --json-out`：

```json
"rerun_offer": {"rounds": 9, "file_rounds": 0, "ceiling_rounds": 9,
                "command": "brian evolve --rerun-unrecorded --with-ceilings --rerun-limit 0",
                "text": "9 round(s) the queue could pay today (...) ...; the queue has paid none of this store's 17 payment(s) ..."}
```

**这一轮顺手改掉了自己刚犯的错**：第一版在 `_rerun_offer` 里又手写了一遍命令字符串，和 `_ceiling_command` 各写一份
——正是第 148 轮刚立的规矩。（同一页上还有一行 `ceiling debt …` 也叫这条命令。）于是把命令收进
`_rerun_command(with_ceilings)`，`_ceiling_command` 也改用它，并且加了一条测试直接比对两条线的命令是同一个字符串。
两行仍然都印命令，因为它们各自要能被单独读、单独执行——但它们出自同一个生成器，不可能是"两个命令"。

**边界 360：页面上的计数只陈述过去；能改变它的那一句（能付几轮、哪条命令）必须和它同页。**否则读者只知道历史，
不知道下一步。

**边界 361：同一命令被两行引用时，必须由同一个生成器产生，并由测试比对这两行。**否则两行会各自漂移成两个命令，
而读者无从知道该敲哪一个。

**边界 362：同一页上的两个数字，如果一个来自历史、一个来自当下，就必须分别命名时间。**`0 by the queue` 是累积的
过去，`9 round(s) … today` 是此刻的状态；不加限定并排印出来，会被读成"今天也做不了"，而那恰恰是第 147 轮那句话
最容易被误读的方向。


## 3.156 「能付几轮」只数付得动的，「付完了」再印一遍

第 149 轮把"今天能付几轮、用哪条命令"写进了页面。这一轮把它搬到命令里——在开始测量之前印一次，结束之后再印
一次——结果在改动的过程中先发现页面自己数错了。

**数错的不是句子，是分母。** `_rerun_offer()` 原来把 `_unrecorded_matters()` 的每一行都算进"能付"，而
`--rerun-unrecorded` 只测**恰好只有一个候选文件**的轮：没有文件的、有两个文件的都会被跳过。真账本上
`_unrecorded_matters()` 返回 47 轮，其中没有一轮有唯一文件；页面当时说的"9 轮"全部来自上限家族。于是同一句
话一边数着 47 轮"今天能付"，一边推荐的命令一轮也付不了它们。第 150 轮把这条过滤写进 offer 本身：

```
    file_rounds = [item for item in matters if len(item[3]) == 1]
    unplaceable = len(matters) - len(file_rounds)
```

能付的说"还有 N 轮说不清文件，没有命令能付它们"；一轮都付不动时直接说
`nothing for the queue to pay today: 47 round(s) keep no unique file, so no command can pay them`。

**同一句话，两处印。** 命令现在在第一次测量之前印一次 offer，在返回之前用同一个函数再印一次，两个标签写明
各自是哪一刻：

```
families          1: 0 round(s) from the file queue, 9 from the ceiling queue
ceilings          9 round(s) name their file and never recorded their ceilings; re-measuring them here (a drain pays this family too)
offer             9 round(s) the queue could pay today (9 that named their file but never recorded their ceilings):
                  brian evolve --rerun-unrecorded --with-ceilings --rerun-limit 0; the queue has paid none of this
                  store's 17 payment(s): every one was typed by hand, so the store says nothing about what the queue
                  would do
walk              18 of the re-run queue's recorded invocation(s); each round it pays names it
...
ceilings          9 of 9 re-measured with their ceilings; 0 still without
left to pay       nothing for the queue to pay today: every round that still decides something records the file and
                  the ceilings it was measured under
```

这不是一次演示：真账本上这一次拖干就是**队列自己付的第一趟**。跑完之后页面第一次把两种付款分开——
`1 by the queue, 17 typed by hand`，`26 round(s) paid in all`，`re-run offer` 也从"9 轮"变成"今天没有可付的"。
在第 147 轮之前，同一本账说的是 `0 by the queue`；那时队列真的没付过一轮，"能付"这句话只活在测试和演示里。
现在它第一次有了行动记录。

三点读法：

1. **"能付"必须和"执行"用同一个过滤。** 页面和命令本来是两个函数，各写一遍条件就会分叉：页面数 9 轮，
   命令付 0 轮，而两边都不算错——错在它们算的不是同一件事（边界 363）。
2. **一句话在两个时刻印，标签就是它的坐标。** `offer` 和 `left to pay` 是同一句 offer 的两个瞬间；不标
   时刻，读者看到的是一句话自相矛盾（边界 365）。
3. **两个数都从一个函数出。** 若"承诺付几轮"和"还剩几轮"各算一遍，测量失败、被挡、或中途停下时它们会各说
   各话，而页面上没有任何东西能指出谁对（边界 364）。

**边界 363**：能付多少轮的计数只能包含有命令能执行的轮；说不清文件的轮必须单独计数，否则页面承诺的是没人
做的工作。**边界 364**：把页面的句子印到命令里的人，必须在开工前印一次、收工后再印一次，且两次来自同一个
函数，否则"它承诺付什么"和"还剩下什么"是两本账。**边界 365**：同一句话出现在两个时刻，就必须有标签说明
是哪一刻，否则一个数字看起来在反驳它自己。

### 3.157 付不动的那一半：把"没有命令能付"从句子里的减法变成页面上的两段

第 71 轮把上限欠账按域数出来，第 72 轮只收"付得动"的那部分，第 74 轮让句子点名付它的命令。三件事都对，合起来
仍然缺一句话。真账本上的欠账行是：

```
ceiling debt      47 of 56 round(s) do not record the ceilings they were judged under, and none of them can be re-run, so the default --max-limit-drift cap stays a warning
```

`none of them can be re-run` 是真的，也是全部信息量。47 轮分两种：一种轮记录了自己的文件、只是没记上限，第 73 轮
之后 `--with-ceilings` 能重跑它；另一种既没记文件、也不再影响任何在册版本或拒付，任何命令都碰不到它，缺的是
**修记录**，不是跑队列。第 150 轮第一次在真账本上真正付掉 9 轮之后，第一种清了，剩下 47 轮全是第二种——句子没说
这件事，读者得自己做减法。

于是这一轮加两处。

**一、欠账行点名付不动的那部分。** `format_ceiling_shortfall` 收第三个计数 `unpayable`，并把它夹到
`missing - payable - named` 之内（句子里的三个数必须能对上账）：

```
ceiling debt      2 of 3 round(s) do not record the ceilings they were judged under; re-running 1 of them would leave 1 without one, so the default --max-limit-drift cap stays a warning; 1 of them are out of reach: no command can pay a round that names no single file, or that no longer decides anything; measure them with brian evolve --rerun-unrecorded --rerun-limit 0
```

`unpayable == 0` 时这段是空串，历史句子逐字节不变——一个新子句只在一个新数字非零时出现，这样旧记录的复述不会被
这一轮改写。三个分支（有可付、无可付有具名、完全付不动）都接上它。

**二、`--unrecorded` 的表分成两段。** 同一张表里，前一类行的下一步是"跑一条命令"，后一类行的下一步是"修记录"，
把两种行排在一起，等于给第一类递了命令、给第二类递了同一个命令：

```
rounds            2 of 3 cannot be attributed; 2 of them still decide something
queue             1 round(s) can be re-run here; 0 already re-measured; the share would fall 67% -> 33%

payable           1 round(s) name exactly one candidate file, so the queue can re-run them

out of reach      1 round(s) name no single file (none, or several), so no command can re-run them while the record stands

domain           round                            likely file                        why it still matters
----------------------------------------------------------------------------------------------------------------------------------
SMD              round-ceilings-missing.json      data/smd/stream-one.json           wrote 1 version(s) still on the page; 1 refusal(s) still carried  [+ceilings]
SMD              round-out-of-reach.json          unknown                            wrote 1 version(s) still on the page  [+ceilings]
```

脚注也跟着分成两句：可付行"重跑一条，名单就短一条"，付不动行"缺的是一条修好的记录，不是一次重跑"。JSON 里
`matters` 之外新增 `payable` / `out_of_reach` 两个列表，`ceilings` 块补 `named` / `unpayable`——取数的人不必自己
再实现一遍"哪一行付得动"的规则。

三个地方（页面、欠账行、payload）现在读同一个 `ceiling_unpayable`，它和另外两个计数一起在分支之前算出来。第一次
把它写在 `--campaign` 分支里，`--unrecorded --json-out` 当场 `UnboundLocalError`——"同一个数在三处出现"这句
话，落地的时候就是"一处算，三处读"。

**三条读法。** 一是"付不动"不是一个数字，是一个分类：读者要决定的是下一步跑命令还是修记录，页面的形状应当替
他答。二是欠账的减法要写在句子里：`47 里 47 付不动` 比 `47` 更能说明这一轮该做什么。三是一个只在新数字非零时
出现的子句，是让旧句子保持不变的最省办法。

**边界 366：欠账计数必须点名"没有任何命令能降低"的那部分。** 让读者自己做减法，等于让读者自己判断一条记录缺陷
和一笔队列欠账的区别，而那正是这个页面存在的理由。

**边界 367：一张表混着两种下一步时，要按"下一步不同"拆开。** 可付的行邀请一条命令，付不动的行邀请一次修记录；
同一张表把它们排在一起，等于建议了一条对一半行无效的命令。


### 3.158 给"付不动"那半配一个动词：把轮记录的文件补回去

第 151 轮把那笔欠账按"读者的下一步"分成了两段，并且诚实地写了第二段缺的是**修好的记录**。写完就发现这句话
有问题：本仓库没有任何命令能把一个轮自己的文件补回去，所以"缺的是修好的记录"其实是在建议手工编辑 JSON。诊断
有了，动词没有——这正是第 108 轮那条教训的第十二次复发（一条命令能指出矛盾，不等于有一条命令能消除它）。

于是这一轮加 `--claim-round-file ROUND=FILE`（配 `--force-claim` 与 `--claim-why`），并把它接进三处：它的
预览、`--unrecorded` 的脚注、欠账行本身。

**三次拒绝，每次都是因为"看起来修好了"比"没修"更坏：**

1. 轮记录必须存在——对一条没人读得到的轮做断言，是对"无"做断言。
2. 文件必须在这个 checkout 里——指着一个读者打不开的路径，修的是文书、丢的是证据（第 92 轮的规则）。
3. 文件必须就是这轮记录下来的几何指向的那一个，除非 `--force-claim`——窗口数、计分窗口数、节奏对不上的文件
   不是它测过的那一个；把它写进去，等于把一个行从"没有命令能付"挪到"有命令能付"，而这件事并不成立。

另外，**已经记了另一个文件**的记录也拒绝（除非 `--force-claim`）：这时记录不是缺一个事实，而是错了一个事实，
"修"和"重测"是两个动词，只有重测能裁决。

几何判定由调用方给（匹配器住在 `cli`，第 51 轮），答案只有三个：`agrees` / `disagrees` / `unknown`。
`unknown` 会被如实记进记录，不会被凑成 `agrees`——不知道不等于同意（边界 369）。

**真账本上的效果**（`var/demo151`：一条付得动的轮、一条付不动的轮）：

```
ceiling debt      2 of 3 round(s) do not record the ceilings they were judged under; re-running 1 of them would
                  leave 1 without one, so the default --max-limit-drift cap stays a warning; 1 of them are out of
                  reach: no command can pay a round that names no single file, or that no longer decides anything;
                  repair a row with brian evolve --claim-round-file ROUND=FILE; measure them with brian evolve
                  --rerun-unrecorded --rerun-limit 0
```

跑一次 `--claim-round-file round-out-of-reach.json=data/smd/stream-one.json --force-claim`（那轮的几何是 7 窗口 /
600 秒，文件是 3 窗口 / 300 秒，所以必须显式越权）：

```
claimed          round-out-of-reach.json  now names data/smd/stream-one.json (geometry disagrees)
claimed          1 record(s) repaired; each keeps the file it named before in stream_path_history, so the repair
                 is visible in the record (boundary 369).
```

同一句欠账行变成：

```
ceiling debt      2 of 3 round(s) do not record the ceilings they were judged under; re-running 1 of them would
                  leave 1 without one, so the default --max-limit-drift cap stays a warning; 1 of them name their
                  file, so --with-ceilings can re-measure them; measure them with brian evolve --rerun-unrecorded
                  --with-ceilings --rerun-limit 0
```

注意两件事：`out of reach` 那一段消失了，**而且 `measure them with` 后面换成了带 `--with-ceilings` 的命令**——
这一行的语义从"没有命令能付"变成了"上限队列能付"，连建议的命令都换了。修记录不是记账，它改变了下一步。

`--unrecorded` 也跟着变短：修之前 `2 of 3 cannot be attributed; 2 of them still decide something`，修之后
`1 of 3 cannot be attributed; 1 of them still decide something`，再跑一次就只剩那句话：
`Every round that still decides something says which file it measured.`——这一段自己消失了，而不是被标记为
"已修"。

**一个当场修掉的缺陷。** 预览的第一版把 `dry_run` 的早退放在几何检查**之前**，于是
`--claim-round-file … --dry-run` 打印了一个真跑会被拒绝的计划（`would claim … (geometry disagrees)` 然后
`exit 0`）。这正是第 135 轮那个缺陷换了位置：预览必须跑完所有检查，只跳过写入。改成"检查全跑、写入包在
`if not dry_run:` 里"之后，同一个预览会以 `cannot claim that record: … does not agree with the geometry` 退出 2。

**三条读法。** 一是"缺的是记录"必须配一条能改记录的命令，否则这句话是让读者去手改 JSON。二是修复要让**类别**
移动，而不只是数字移动：这里欠账数没变（还是 2 of 3），变的是它由哪一族付、以及页面建议哪条命令。三是"人员
断言"与"几何证据"冲突时，两者都要进记录（`geometry: disagrees` + `--claim-why`），否则下一位读者会以为匹配器
同意过。

**边界 368：修复必须先跑完所有检查，只跳过写入。** 一个打印出真跑会拒绝的计划的预览，比没有预览更坏——它把
"计划"和"承诺"混成一件东西。

**边界 369：人员断言要带它自己的判定。** 记录里写 `agrees` 还是 `disagrees` 不是措辞问题：前者说几何支持这次
修复，后者说有人越过了几何，两者对后来者的含义完全不同。

**边界 370：修一条记录可以改变"哪一族付得起"，却不改变欠账的数字。** 只说"欠账 2 of 3"会把一次类别迁移说成
什么都没发生；句子要跟着说"这 1 条现在由上限队列付"。

### 3.159 一轮站在"人指的、几何不认"的文件上：测之前要说，页面上要数

第 152 轮给"队列够不着的那一半"装了一个动词 `--claim-round-file`，并且让这个动词把**自己的判定**写进记录：
`agrees` / `disagrees` / `unknown`。判定是 `disagrees` 的时候，这一轮就走进了一个它从没测过的家族——
而在此之前，没有任何地方说过这件事。恰恰在两个最要紧的时刻没有说：

1. 队列真的在这个文件上重测它的时候——那一轮会带着新数字退休旧记录，读者会以为新数字长在那条轮原本的文件上；
2. 页面把它算进"队列付过的轮"、或者按文件给它排队的时候。

**一个事实，两个位置。** `claim_note(record)` 回答"这条记录现在指着的文件，是不是一个人指过去、而几何不认的"；
`claimed_rounds(rounds)` 把这件事按轮列出来，给页面用。两个函数都读 `stream_path_history` 的最后一条，
而且只认**指向当前文件**的那一条：记录后来又换了文件时，旧断言说的是另一份证据，不读。

真跑一次（`var/demo153`，一条被手工指到 `data/smd/stream-one.json`、几何 `disagrees` 的记录）：

```
---- re-running round-out-of-reach.json on data/smd/stream-one.json
  careful: the file data/smd/stream-one.json was claimed by a person, and its geometry disagrees with the
  ones the round recorded -- what this measures is evidence about that file, not about the one the round
  first measured (boundary 371)
recorded round var\evolve\round-20261120T085517Z.json
retired round-out-of-reach.json (re-measured as round-20261120T085517Z.json)
  ceilings         the successor records the ceilings it was judged under
ceilings          1 of 1 re-measured with their ceilings; 0 still without
```

页面上（`var/demo151`，同一份记录在被重测之前）：

```
claims            1 round(s) name a file a person claimed, and the geometry never confirmed it:
                  round-out-of-reach.json data/smd/stream-one.json (disagrees)
claims            a re-measurement on such a round is evidence about that file, not about the one the
                  round first measured (boundary 371)
```

**`unknown` 不是 `agrees`。** 判定的三种值里，`unknown`（那个文件不在本 checkout 里、几何无从比对）和
`disagrees` 一样都算"没被确认"：读这句话的人需要知道"这是人说的"，而不是"机器核过的"。反过来，只有
`agrees` 才是沉默——沉默必须有依据。

**第一次把这句话接错了位置。** 最初的版本把警告印在 `---- re-running …` 之后、`if not recorded:` 之前，
于是 `var/demo151` 那次跑出来的是这样：

```
---- re-running round-out-of-reach.json on data/smd/stream-one.json
  careful: the file data/smd/stream-one.json was claimed by a person, …
ceilings          0 of 1 re-measured with their ceilings; 1 still without
```

警告说"这次测的是什么"，然后什么都没测——那条记录没有候选定义，队列在量之前就返回了。**页面可以警告一次
测量，但不能描述一次不会发生的测量**（边界 372）。修法是把警告移到"这条记录确定可以重测"之后，并且把这句话
抽成 `_claim_note_line(record)`，这样测试能直接钉住顺序，而不用等一次五分钟的测量。

**顺手修掉两处容错。** 为了造这条演示，往账本里写了一条 `trials` 只有名字（而不是"候选 + 测量 + 判定"三段）
的旧形状记录，`rejection_provenance` 直接抛 `AttributeError: 'str' object has no attribute 'get'`——两处：
`trials` 的条目本身是名字，以及条目的 `verdict` 是名字。两处都改成"跳过这条、继续数"，因为一个只想要计数的
页面没有理由因为一条读不懂的旧记录而崩掉（边界 153 的同一条规则；第 71 轮 `accepted` 是整数的那次是同一个坑）。

**这一轮也留下了一个缺口。** 重测之后，新记录直接写 `stream_path`，**不继承**被重测记录上的那条断言：上面
`var/demo153` 第二次 `--campaign` 就不再印 `claims` 行。也就是说，"这个文件是人指的、几何不认"这个事实，
在一次成功的重测之后就消失了——而那次重测恰恰是站在它上面的。这写进了下一项。

**边界 371**：一次测量的前提如果是一个人的断言（而不是机器核过的几何），那么在这句话起作用的地方——重测
之前、页面计数时——必须说出来；只写进那条命令当时打印的一行日志，等于只对当时在场的人说过。
**边界 372**：警告的位置是它内容的一部分：对一次不会发生的测量说"这次测的是什么"，比不说更坏，因为它让
读者以为工作做了。凡是"先说后做"的句子，都要放在"确实会做"的分支里。


### 3.160 重测要继承它踩着的那条断言：`inherited_from` 这一栏

第 153 轮让"这个文件是人指的、几何不认"在重测之前说出来、在页面上数出来。但那次重测**写出的新记录**
只写 `stream_path: <file>`，一句来历都没有：于是一次成功的重测之后一分钟，账本里那条断言就消失了——
`--campaign` 的 `claims` 计数掉一，而实际上什么都没被修掉。**修的是文件，丢的是证据**。

真跑一次（重跑 `var/demo153`，那条被手工指到 `data/smd/stream-one.json` 的记录）：

```
---- re-running round-out-of-reach.json on data/smd/stream-one.json
  careful: the file data/smd/stream-one.json was claimed by a person, and its geometry disagrees with the
  ones the round recorded -- what this measures is evidence about that file, not about the one the round
  first measured (boundary 371)
recorded round var\evolve\round-20261120T090613Z.json
retired round-out-of-reach.json (re-measured as round-20261120T090613Z.json)
ceilings          1 of 1 re-measured with their ceilings; 0 still without
```

新记录的 `stream_path_history` 现在是：

```
[{"from": "data/smd/stream-one.json", "to": "data/smd/stream-one.json",
  "at": "2026-11-20T09:06:13.294446+00:00", "geometry": "disagrees",
  "why": "the record named no file; this one was claimed by hand",
  "inherited_from": "round-out-of-reach.json"}]
```

于是同一份账本上的页面（重测之后）：

```
claims            1 round(s) name a file a person claimed, and the geometry never confirmed it:
                  round-20261120T090613Z.json data/smd/stream-one.json (disagrees)
```

注意这一行里的轮名已经换了：数的是 `round-20261120T090613Z.json`，也就是**真的在那个文件上量过**的那一轮，
而不是当初被指的那一轮。这就是继承的作用——断言跟着证据走，而不是跟着它第一次被写下的那条记录。

**`from` 和 `to` 一样，所以这一栏必须存在。** 继承下来的条目里，`from` 和 `to` 都是同一个文件：一条"重测"
测的就是这条记录原本指的那个文件，方向没变。单看 `from`/`to`，这看起来像一次什么都不做的搬动；只有
`inherited_from` 说出它是从哪条记录接过来的。**同一个字段在两个地方的语义不同，就得有一个字段专门说方向**。

**时间是新的，来历是旧的。** 条目的 `at` 用新记录的时刻，"一个人指过这个文件"是文件的属性，而"这一轮在它
上面量了"是今天的属性——两个事实都有日期，混用其中一个就会让旧断言看起来像刚发生的（第 153 轮记 `at`
时踩过同一类）。

**`to_dict` 才是真正的入口。** `persist` 从 `RoundResult.to_dict()` 写轮记录，所以字段只加在 dataclass 上、
不写进 `to_dict`，下一张页面就读不到——测试里那句"a field missing there is a field the next page cannot read"
就是这件事。没有继承记录的轮仍然把这个键写成 `None`（而不是 `[]`），否则读者要自己区分"没有断言"和"有一条
空的断言"。

**遗留的缺口（写进下一项）**：继承可以链式发生，但条目只记得**上一跳**（`inherited_from`）。连着继承几次之后，
"这条断言最初是谁在什么时候记下的"就查不到了——断言越老、越像是在这一轮刚被确认的。下一轮让它带上
`claim_origin`（最初那条断言所在的轮）与链长，并在页面上说出它已经站了多少轮。

**边界 373**：一次测量如果是踩着某条人的断言完成的，那条断言必须落到它写出的记录上；否则修完一次，证据
就随旧记录一起退休了，而页面会把"证据消失了"读成"问题解决了"。
**边界 374**：继承来的事实带两个日期——它发生过的时刻、和它被再断言一次的时刻；只记其中一个，都会让读者
把旧断言当成新证据（或把新测量当成旧结论）。

### 3.161 断言的年龄：同一句话被继承两次之后，`at` 已经不是它的出生日

第 154 轮把断言复制到了重测写出的新记录上，但那条拷贝只记得**一跳**（`inherited_from`），而 `at` 永远
被刷新成新记录的时刻。于是连着重测两次之后，"这条断言是什么时候第一次被记下的"就查不到了：一条越来越老的
断言，看起来和这一轮刚确认的一样新。

这一轮让它带上自己的年龄：`claim_origin`（最初记下这条断言的轮）、`claim_since`（那一刻）、
`claim_hops`（被继承了几次）。

真跑第二次继承（`var/demo155`，先把上一轮的继承记录补上 `claim_*` 三个字段——见下面的"遗留"）：

```
---- re-running round-20261120T090613Z.json on data/smd/stream-one.json
  careful: the file data/smd/stream-one.json was claimed by a person, and its geometry disagrees with the
  ones the round recorded -- what this measures is evidence about that file, not about the one the round
  first measured (boundary 371)
recorded round var\evolve\round-20261120T091408Z.json
retired round-20261120T090613Z.json (re-measured as round-20261120T091408Z.json)
```

新记录的条目：

```
{"from": "data/smd/stream-one.json", "to": "data/smd/stream-one.json",
 "at": "2026-11-20T09:14:08.985899+00:00",           <- 这一轮量它的时刻
 "geometry": "disagrees", "why": "the record named no file; this one was claimed by hand",
 "inherited_from": "round-20261120T090613Z.json",    <- 上一跳
 "claim_origin": "round-out-of-reach.json",          <- 最初那条断言
 "claim_since": "2026-11-20T09:06:13.294446+00:00",  <- 没有被刷新
 "claim_hops": 2}
```

页面：

```
claims            1 round(s) name a file a person claimed, and the geometry never confirmed it:
                  round-20261120T091408Z.json data/smd/stream-one.json (disagrees)
claims            the one on round-20261120T091408Z.json was inherited 2 time(s) and was first recorded on
                  round-out-of-reach.json at 2026-11-20T09:06:13.294446+00:00
```

**两个指针，不是两个字段的重复。** `inherited_from` 回答"这条记录重测的是哪一条"，`claim_origin` 回答
"这条断言最初记在哪儿"。第二次继承之后两者不同（前者是上一跳，后者是最初那条），一个字段不可能同时是这两个
答案——这就是为什么必须两个都留。

**`claim_since` 不能跟着 `at` 走。** 每次重测都会刷新 `at`（那是测量的时刻），而断言的年龄不是测量的年龄。
只要有一次把 `claim_since` 换成 `at`，"这条断言从第 N 轮起就没被几何确认过"就永久丢了（第 153 轮记 `at`
时，`boundaries 374` 说的是同一件事的另一半：一个事实带两个日期，混用哪个都会让读者读错）。

**页面上的句子只在真的继承过时才出现。** `hops == 0`（断言就是这一轮自己记的）时不多说一句——多一句"它是
这一轮记下的"只是把读者已经知道的事重复一遍，而且会让真的继承（那一句）看起来像套话。

**遗留（写进下一项）**：第 154 轮写出的记录没有这三个字段，从现在开始重测才会写。所以旧的继承记录在页面上
读成 `hops = 0`——它们的链从 155 轮才开始数。这一轮只能靠 `inherited_from` 推出一跳的 `claim_origin`，
`claim_since` 则会误取那次重测的时刻。下一轮给这些记录补一次（自报 `backfilled`，与第 147 轮同形）。

**边界 375**：被复制的事实必须带上自己的年龄；如果记录里的时刻每次都被刷新，一条旧断言就会读得像刚被确认的，
而这正是"没有外部信号时自我确认"最省力的一种形态。
**边界 376**：一条链需要两个指针——上一跳（"这条重测了什么"）与原点（"这句话最早是谁、在哪一轮说的"）；
一个字段同时承担两种语义，迟早会被后来的代码按其中一种解读。

### 3.162 补写一条更老的断言：能推导就推导，推不出就写"未知"

第 155 轮让断言带着 `claim_origin` / `claim_since` / `claim_hops` 一路传下去。但**第 154 轮写出的记录没有这三个
字段**，它们只有 `inherited_from` 和一个被刷新过的 `at`。于是那些记录的链从第 155 轮才开始数，而 `claim_since`
会退回到**那次重测**的时刻——正是这个字段存在的目的所要防的那种混淆：一条更老的断言，拿到一个更新的日期。

这一轮给它们补上，并且分两步：**能推导的推导，推不出的写"未知"**。

推导规则很简单：条目说它 `inherited_from` 某条轮记录，就去那条记录里找这条断言的时刻（它自己的 `claim_since`，
或者它的 `inherited_from`，一路往回走）；没有 `inherited_from`（断言就是那一轮自己记的）就用它自己的 `at`。
走不到（记录被裁掉、改名、损坏）时写 `claim_since: ""` 加 `claim_since_source: "unknown"`，**不回填一个像样的
替代值**。

真跑（`var/demo156`，就是 154 轮那个 demo，记录还没被 155 修过）：

```
would fill       round-20261120T090613Z.json  1 entr(y/ies): 1 dated from the record they point at, 0 unknown
would fill       round-out-of-reach.json  1 entr(y/ies): 1 dated from the record they point at, 0 unknown
nothing was written; drop --dry-run to complete the chain (boundary 304).
```

补完之后页面：

```
claims            1 round(s) name a file a person claimed, and the geometry never confirmed it:
                  round-20261120T090613Z.json data/smd/stream-one.json (disagrees)
claims            the one on round-20261120T090613Z.json was inherited 1 time(s) and was first recorded on
                  round-out-of-reach.json at 2026-11-20T08:38:50.882396+00:00
```

注意那个 `08:38:50`：**它是从被指的那条记录里推导出来的**，不是这次重测的 `09:06:13`。补写之前，这一行要么
干脆不出现（`hops = 0`），要么会把重测时刻当成断言年龄。

再跑一次：

```
nothing to backfill: every claim chain already carries its origin, its first moment and its hop count
```

**真实账本上是"无事可做"**：这台机器上还没有任何一条记录带过 `stream_path_history`（那些断言都只活在 demo 与
测试里）。所以这一轮的能力由 fixture 与 demo 证明，不由实账本证明——这一点必须说出来，否则"补完了"会被读成
"实账本被清理过"。

**每一条被补的条目自己说 `backfilled: true`。** 一次修补产生的"完整"，不能读起来像是这条链一直就是完整的
（第 147 轮的 `--backfill-walks`、第 83 轮的同一条规矩）。

**边界 377**：后加的字段会把历史读成"缺省值"；补写时必须把**补过什么**记在补过的位置，否则修复成功的那一刻，
"这条链本来就是这样"就成了唯一的记录。
**边界 378**：一个补写无法推导的日期只能写"未知"，不能填一个看起来合理的替代值——provenance 字段里的错日期
不会显得可疑，它会被当成证据。

### 3.163 修补出来的年龄，不能变成"本来就带着的年龄"

第 156 轮补完了旧记录的链，并在每条被补的条目上写了 `backfilled: true`。但页面照旧把那个日期当成"记录一直带着
的日期"打出来——修补成功的那一刻，修补就成了唯一记录（第 83/147 轮说过同一件事：修补产生的"一致"从来不是第二个
证人）。

这一轮让**修补本身跟着事实走**：继承时，如果被继承的那个日期是补出来的，后继条目就继承"它是补出来的"这件事，
而不是只继承日期。新键 `claim_since_source`（`derived` / `unknown` / `backfilled`），页面对 `backfilled` 的那一行
加一句括号说明。

真跑（`var/demo157`，第 156 轮补过的 demo 再次重测）：

```
---- re-running round-20261120T090613Z.json on data/smd/stream-one.json
recorded round var\evolve\round-20261120T092858Z.json
retired round-20261120T090613Z.json (re-measured as round-20261120T092858Z.json)
```

新条目：

```
{"at": "2026-11-20T09:28:58.642337+00:00", "inherited_from": "round-20261120T090613Z.json",
 "claim_origin": "round-out-of-reach.json", "claim_since": "2026-11-20T08:38:50.882396+00:00",
 "claim_hops": 2, "claim_since_source": "backfilled"}
```

页面：

```
claims   the one on round-20261120T092858Z.json was inherited 2 time(s) and was first recorded on
         round-out-of-reach.json at 2026-11-20T08:38:50.882396+00:00 (that date was completed by a repair,
         not carried by the record)
```

注意括号里的那句话跨了两跳还在：`round-out-of-reach.json` 那次记录是真的，`08:38:50` 也是真的，但**它是被
`--backfill-claims` 从被指记录里推导出来写回去的**，不是那条记录当年自己带着的。一次修补的证据，会跟着它修出来的
那个事实到处走——包括走到两跳之后。

**"未知"也要能传。** `unknown` 的日期同样会被继承（`claim_since: ""` 加 `source: "unknown"`），页面显示成
`at ?`：一条链上只要有一次无法确定，后面所有继承都带着这个不确定，而不是靠某次重测的 `at` 把它"填满"。

**边界 379**：修补产生的事实必须把"它是修补出来的"带给它的每一个后继；只在被修的那条记录上盖一个章，
下一跳就会把修补读成原始记录。
**边界 380**：一个推导出来的数字，页面必须同时说出它有多少是推导的；否则"完整"和"被补完"在页面上无法区分。

### 3.164 历史页的每一行，也要说出它站在谁的断言上

第 153 轮在重测之前说出来，第 155 轮让它带上年龄，第 157 轮把"修补出来的年龄"标出来——**历史页什么都没说**。
而读者排错时最先打开的就是这一页：一行看起来和别的行一模一样，实际它测的那个文件是某个人手工指过来的，几何并不认。

这一轮把标记放到行上。`claim_mark(record)` 产出短语，`--history` 把它接在文件标记之后：

```
 3  …  [data/smd/stream-one.json]  [rests on a file a person claimed]
 5  …  [data/smd/stream-one.json]  [rests on a file a person claimed, inherited 1 time(s)]  [paid by the ceiling queue, walk 2]  [replacing round-out-of-reach.json]
 6  …  [data/smd/stream-one.json]  [rests on a file a person claimed, inherited 2 time(s), its age was completed by a repair]  [paid by the ceiling queue, walk 3]  [replacing round-20261120T090613Z.json]
```

三行正好是三段历史：3 是原始断言（没有继承，也没有被补过），5 继承了一次，6 继承两次、而且年龄是补出来的。
**读者不需要打开任何 JSON 就能看出这三行不一样。**

**标记必须是最短的短语，而不是一段话。** 行上已经有文件、队列、走次、替换关系；再塞一个段落，"扫行"这件事就没了。
所以 `claim_mark` 只回答三件是/否：有没有一条人指的断言（`rests on a file a person claimed`）、继承了几次、
年龄是不是补出来的。**`agrees` 不标**——几何确认过的断言不是警告，是正常的证据。

**标记挂在行上，而不是列表上方的一段说明。** 第 121 轮起，凡是"某一行特殊"的事实都写在行尾；一段总述会让读者
在总述和行之间来回找，而页面无法保证他先读到总述。

**边界 381**：一个会改变"这一行该怎么读"的事实，必须出现在这一行本身；在产生那一行的终端里说一次，只对当时
正好在看的那个人生效。
**边界 382**：会越长的标记（断言、继承次数、修补年龄）要保持为行尾的一个短语；把它写成列表上方的一段话，
等于要求读者按页面无法强制的顺序阅读。

### 3.165 头部要数，而且要把"行"和"断言"分开数

第 158 轮把标记放到每一行上，但读者要滚完 74 行才能数出"有几行站在人的断言上"——**给了细节，没给事实**。
这一轮让历史页的头行自己数：

```
claims            3 of 6 row(s) rest on a file a person claimed (2 inherited at least once, 3 whose claim columns a repair wrote)
claims            2 of them were retired into a successor, which carries the assertion on
```

**"行数"不是"断言数"。** 第二行必须存在：一条断言被重测继承后，旧行退休、新行接住它，两行站在**同一条**断言上。
只数第一行就会把同一条断言数两遍，而读者看不到这件事——所以退休的那些要在同一页上单独数一次。

**数什么，名字就得说到那么窄。** 我第一版写的是 `3 with an age completed by a repair`，但 `backfilled` 的真实含义是
"这条条目的 `claim_*` 栏是修补写的"。原始那条记录（`round-out-of-reach.json`）的日期本来就是它自己的 `at`，
修补只是把栏补上，并没有"修出一个年龄"。名字写宽了，读者就会把"补过栏"当成"年龄不可信"（第 380 轮的边界反过来咬人）。
现在写 `whose claim columns a repair wrote`，并且测试里钉住"不能出现 completed by a repair"。

**没有断言时整行不出现。** 一个 `claims 0 of 74` 的头行不会告诉读者任何东西，只会让人去找"那我该关心什么"。

**边界 383**：行的计数不等于断言的计数——退休行把断言交给了后继，所以头行必须两个数都说，否则同一条断言被数两遍。
**边界 384**：一个计数的名字不能宽于它数到的东西；`backfilled` 数的是"哪些栏是补写的"，不是"哪些日期是修出来的"。

### 3.166 一个数字印在两页上，就只能由一处算

第 158 轮把标记放到行上，第 159 轮让历史页的头行数出来——而 `--campaign` 从第 153 轮起就在用自己的措辞数同一件事：
历史页说 "row(s)" 并把退休行单独数，战报页说 "round(s)" 且数不到退休行。措辞已经开始分叉，再走几轮两个页面就会
用两种说法描述同一本账。

这一轮把计数搬进 `campaign.claim_counts(rounds, population=...)`，返回 `ClaimCounts`（`rows` / `total` /
`inherited` / `repaired_columns` / `retired` / `population`），两页都印它的 `sentence()`：

```
# --campaign（population = 活着的轮）
claims            1 of 3 live round(s) rest on a file a person claimed and the geometry never confirmed it (1 inherited at least once, 1 whose claim columns a repair wrote)
claims            the rows: round-20261120T092858Z.json data/smd/stream-one.json (disagrees)

# --history（population = 列出的行）
claims            3 of 6 row(s) rest on a file a person claimed and the geometry never confirmed it (2 inherited at least once, 3 whose claim columns a repair wrote)
claims            2 of them were retired into a successor, which carries the assertion on
```

**同函数，两个口径，各自报名。** 两页数的**不是同一个总体**：历史页列全部行（含退休的），战报页只数列活的轮。
所以句子必须把总体写进自己那一句里——`live round(s)` 与 `row(s)`。同一个数字在两种口径下不相等不是矛盾，
但**不报名**就会读成矛盾（第 80 轮的"两列数的是不同东西就要标注"往上挪一层：两页数的是不同总体）。

**分叉是这么发生的。** 第 153 轮给战报页加了一句，因为它那里有 `claims` 列表；第 159 轮给历史页加了一句，因为
它那里有 `rounds`。两次都对，合起来就成了两种措辞。搬进一个函数之后，`sentence()` 同时保住两边要的那半句
（"and the geometry never confirmed it" 来自战报，"inherited at least once / whose claim columns a repair wrote"
来自历史页），并且**测试比对的是两页输出的那句话本身**：把 `live round(s)` 换回 `row(s)` 之后必须逐字节相等。

**边界 385**：同一个量印在两页上时，只能由一处算；两处各写一次总会向"最近一轮需要的那半句"漂移。
**边界 386**：两页用同一句话数不同总体时，每页都要在句子里报出它的总体名；否则读者看到的是矛盾，而不是两个问题。

### 3.167 页面印了计数，payload 就要带上计数：让测试用 payload 重建那句话

第 160 轮把计数收进 `ClaimCounts`，两张页面印同一句话。但 `--campaign --json-out` 里只有行列表
（`claims` 是 `[{round, stream_path, geometry, …}]`），没有计数、没有总体名——机器读者想拿"几行"这个数，
只能自己再数一遍。**再数一遍就是第二个实现**，而第二个实现迟早会和它旁边那句话不一致。

这一轮让两份 payload 都带上页面印的那个对象：

```
# --campaign --json-out c161.json
campaign: {'rows': 1, 'total': 3, 'inherited': 1, 'repaired_columns': 1, 'retired': 0,
           'population': 'live round(s)'}
# --history --json-out h161.json
history : {'rows': 3, 'total': 6, 'inherited': 2, 'repaired_columns': 3, 'retired': 2,
           'population': 'row(s)'}
```

**测试的判据不是"字段存在"，而是"payload 能把它旁边那句话重建出来"。**

```python
assert ClaimCounts(**payload).sentence() == printed.split("claims            ", 1)[1]
assert ClaimCounts(**payload).retired_sentence() == printed[1].split("claims            ", 1)[1]
```

`ClaimCounts(**payload)` 能直接接回来，是因为 `to_dict()` 的键和 dataclass 的字段一一对应——这本身就是一条约束：
**payload 的键名不许和字段名分叉**，否则"把 payload 喂回去"这件事做不到，而做不到就没人会发现它们已经不同。

**一处算，三个读者。** 战报页现在是 `campaign_counts = claim_counts(live_records, population="live round(s)")`
算一次，页面印它、payload 取 `to_dict()`、测试比对共享的那句；历史页同理用 `history_counts`。第 160 轮的经验在这里
变成机制：不是"记得用同一个函数"，而是**同一份对象在三个地方被读**。

**顺手补上的那一栏**：行列表仍然留着（`claims`），因为"哪几行、各自几何判决是什么"是 payload 里唯一的逐行事实，
计数不能替代它；反过来，计数也不能从行列表里现数——行列表只列"有断言的"，而总体的分母（`total`）在它之外。

**边界 387**：页面印了某个计数，它对应的 payload 就必须带同一个对象；只带底层行，等于把计数交给每个调用方各写一遍。
**边界 388**：payload 的数字要能原样重建页面上那句话——"同一处算"这件事，只有这样才可测试；字段存在不算证据。

### 3.168 报告页的 payload 也走同一个写手：一句"记录还在库里"对报告是假话

第 141 轮把八处 `--json-out` 写盘收进 `_write_payload`：建父目录、原子替换、印 `wrote <path>`、写不成时说清楚
并非零退出。但 `--history` 和 `--campaign` 写在那个规则之前，一直自己 `Path.write_text` 再自己印一行 `wrote`。
于是：

```
--campaign --json-out nodir\x.json   ->  FileNotFoundError（traceback）
--campaign --json-out x.json         ->  "wrote x.json" 印两遍
```

`grep` 一遍 `_write_payload(args.json_out` 会看到**六**处调用——我原本以为只有那一处，实际同一句式在别的命令里也出现，
所以我第一次的补丁锚点 `assert count == 1` 直接失败（这次失败救了我：说明"一处写手"从来没被盘点过）。

改完之后两页都走同一个写手，`wrote` 只印一次。**但随后又发现第二件事**，是这次真跑出来的：

```
cannot write the payload to .: [WinError 5] 拒绝访问: '..tmp' -> '.'
                what this command recorded is already in the store: only the payload is missing
```

`--history` 什么都没记录——它只是一张报告。第 141 轮那句话是为"已经改了库、只丢了导出"的命令写的，套在报告页上
就成了**对一件没发生的事作证**。于是 `_write_payload` 多了一个参数 `recorded: bool = True`，两页传 `False`：

```
cannot write the payload to .: [WinError 5] 拒绝访问: '..tmp' -> '.'
                nothing was recorded: this command only reports, so only its export is missing
exit=2
```

**成功时也验证过**：`--campaign --json-out deep\nested\c162.json` 建了两级目录，输出里 `wrote` 恰好一行
（测试数的是行数，不是"包含"）。失败时页面已经把报告印完才失败——**报告不是事务**，这是报告页与四条结算命令的
根本区别，也是这句结尾必须换掉的原因。

**边界 389**：一条写给某一族写手的规则，会被这族之外的写手继续违反；"所有 payload 都走同一个写手"这句话只能靠
盘点来证明，不能靠记得。
**边界 390**：失败时的说明必须对**这条命令**为真——"记录还在库里"落在报告页上就是为没发生的事作证；一句关于错
对象的真话，比一句笼统的话更坏。
**边界 391**：写手已经印过的输出，调用方不许再印一次；重复的成功行正是"调用方不知道写手做了什么"的证据。

### 3.169 盘点写手：一句"都走同一个写手"只能靠数出来

第 141 轮把八处 `--json-out` 收进 `_write_payload`，写下一句"这是唯一的写法"。第 162 轮发现两页还在自己写。
这一轮按第 149 项的要求**盘点**，`grep` 的结果是：`_write_payload` 调用点 **15** 处，而自己写盘的还有 **8** 处——

| 位置 | 命令 | 它是什么 |
| --- | --- | --- |
| `src/brian/cli.py:2374` | `--set-domain-limit` / `--set-domain-drain` | 记录型（改状态） |
| `:3383` | `--unrecorded` | 报告 |
| `:3543` | `--sensitivity` | 报告 |
| `:3559` | `--withheld` | 报告 |
| `:3640` | `--audit` | 报告 |
| `:5386` | `run` 的汇总 | 记录型（写了事件与报告） |
| `:5448` | `evaluate` | 记录型 |
| `:6030` | `--summary-out` | 报告 |

全部改走写手之后：**0** 处 `write_text(_json` / `Path(args.json_out).write_text`，`_write_payload` 调用点 **22** 处。
两个数字都被钉进测试：禁写清单为空、调用点等于常量 `EXPECTED_PAYLOAD_WRITERS = 22`。加了新命令却没走写手，
失败信息会直接说"某条命令开始或停止写 payload 了"。

**为什么 22 轮都没发现。** 每一轮修的是"它正在看的那一族"：141 修结算命令，162 修两张页面，两次都只扫了名字
里带 `settle` 或 page 的调用点。规则的对象不是"命令名"，而是**那个动作**——"谁把 payload 写进文件"。所以盘点要按
动作扫：`write_text(` 后面接 `_json(`，或任何 `Path(<某个 out>).write_text`。

真跑一轮验证（`var/demo157`）：

```
--audit --json-out deep\nested\a163.json  -> wrote deep\nested\a163.json          （一次，父目录自动建）
--audit --json-out .                      -> cannot write the payload to .: [WinError 5] 拒绝访问: '..tmp' -> '.'
                                             nothing was recorded: this command only reports, so only its export is missing
                                          exit=2
```

第 162 轮给写手加的 `recorded` 参数在这里显出了用处：八处里有六处是报告、两处改状态。报告页失败时不能声称
"记录还在库里"，改状态的那两处则必须这么说——**同一个写手，两种诚实的说法**，由一个布尔量选择，而不是由调用方
各写一句。

**边界 392**：一条"都走同一个写手"的规则要按**动作**扫描代码库来执行，不能按调用方的名字回忆；每轮修它眼前的
那一族，正是它 22 轮都不完整的机制。
**边界 393**：共享的写手要能对两类调用方分别说真话（改过库的 / 只做报告的）；一个布尔量加一句各自的说明，
比让每个调用方自己写失败信息好，因为后者总会写成"_我这里_看起来发生了什么"。
**边界 394**：只做报告的命令失败时，非零退出说的是"导出没写成"，不是"没干活"；这两件事必须分别说清，否则下一次
读日志的人会把一张已经印完的报告当成一次失败的运行。

### 3.170 清单要能印出来：哪条命令导出什么，失败时说的是哪一句

第 163 轮把"只有一个导出器"这条规则做成了一个**计数**：源码里 `_write_payload(` 的调用点必须是 22 个，多一个
少一个都让测试红。这守住了"下一个写 payload 的人会知道"，但守不住**读**的人：一条规则如果只活在一次
`pytest` 里，那么它是给跑测试的人看的，不是给要用这些命令的人看的。这一轮把同一份清单做成一个页面。

```
$ brian evolve --payload-writers
brian payload writers: 22 command(s), one writer

  --set-domain-limit / --set-domain-drain    records                 {"recorded", "cleared", "in_force", "files"}
  --history                                  reports                 {"rounds", "links", "in_force_clearings", ...}
  --claim-round-file                         records, unless --dry-run {"claimed", "claim_refused"}
  --backfill-claims                          records, unless --dry-run {"filled", "dry_run"}
  --unrecorded                               reports                 {"rounds", "unattributed", "matters", ...}
  --campaign                                 reports                 the campaign report (drift, debt, queue, claims, offer)
  ...
  --settle-reaffirm                          records                 {"reaffirmed", "reaffirm_refused", "reaffirm_skipped"}
  --repair-links --apply                     records                 {"repairs"}
  run                                        records                 {"summary", "windows_judged", "rca_report_ids"}
  evaluate                                   records                 the evaluation report plus its recommendation
  dataset --summary-out                      reports                 the dataset summary

the file-level forms --settle-reaffirm-file, --settle-restore-file, --settle-drop-file, --settle-reanchor-file write
the record list of their single form: one writer, because the single form is the one that writes it.

--backfill-walks takes --dry-run and exports nothing: it writes no payload at all, so a caller cannot
script it the way it can script --backfill-claims (round 164 found this by writing the table).

Every one of them goes through _write_payload: the parent directory is created, the file is renamed
into place, and the page says `wrote <path>` (boundary 336). A write that fails exits 2 and says which
kind of failure it was (boundaries 390/394):
  changed a book  what this command recorded is already in the store: only the payload is missing
  only reported   nothing was recorded: this command only reports, so only its export is missing
                  -- also what a `records, unless --dry-run` row says while it previews (boundary 395)

A command that starts or stops writing a payload fails tests/test_payload_inventory.py until this
table and its count move together (boundary 392).
```

这一页只有 22 行，但它把三件事同时钉住了：

1. **同一份清单的两个读者。** 表格、测试里的 `EXPECTED_PAYLOAD_WRITERS` 和源码里的调用点必须是同一个数
   （`test_the_page_and_the_count_are_the_same_inventory` 三个一起比）。谁加了一条命令却只改了一处，页面和
   测试里总有一处会说谎。
2. **`recorded=` 不是命令的属性，是分支的属性。** 写这一页时发现 `--claim-round-file` 和
   `--backfill-claims` 在 `--dry-run` 下**什么都没记**，但它们传的是默认的 `recorded=True`——于是导出失败时
   页面会告诉用户"命令改动已经写进账本了，只差导出文件"，而实际上什么也没写。改成 `recorded=not dry`：
   一句话的真假要看**这一次执行**做了什么，不是看这条命令归哪一类（边界 395）。实测：

   ```
   $ brian evolve --claim-round-file nope.json=data/smd/stream-one.json --dry-run --json-out .
   cannot claim that record: no round record for nope.json: var\evolve\nope.json
   nothing was written; drop --dry-run to record the claim (boundary 304).
   cannot write the payload to .: [WinError 5] ...
                   nothing was recorded: this command only reports, so only its export is missing
   ```
3. **写清单本身会找到清单里没有的东西。** 表里必须覆盖 `--dry-run` 接受的每一个家族成员，测试逐个比对
   `BATCH_PREVIEWS`。写到这里就卡住了：`--backfill-walks`（第 147 轮）根本没有 `--json-out`——它可以预览、
   可以打印，但脚本拿不到它的结果，而它的姊妹命令 `--backfill-claims` 有。这个缺口不是读代码读出来的，是
   **填表填出来的**（和"取一次清单才知道规则从未被数过"是同一件事，边界 389）。缺口写进页面，也写进下一项。

四个家族成员（`--settle-*-file`）单独一行说明：它们把自己那一组记录交给单条形式，由单条形式写唯一的那份
payload——所以它们是那一行的读者，不是第二十二个之后的新导出器。**文件级命令的导出，是单条命令的导出。**

**边界 395**：共享导出器的 `recorded` 必须跟着**这一次执行**的行为走，而不是跟着命令的分类走——一次预览
什么都没记，就不能说"改动已经在账本里了"。
**边界 396**：只钉在测试里的规则，对需要它的人是隐形的；把清单印在命令旁边，它才同时是约束和说明书。


### 3.171 清单自己能被检查：`--check`、补上的那个缺口，以及"两种什么都没有"

第 164 轮把导出清单做成了一页，并且发现 `--backfill-walks` 根本不导出任何东西。这一轮做两件事：让那一页的
声明**可以被一条命令检查**，以及与它同一件事的另一面——把缺的那个导出补上。

**一、页面的话，命令来验。** 第 163 轮的计数活在一次 `pytest` 里：它只对跑测试的人说话。CI 里的其它检查、
发布脚本、想先确认再相信页面的读者，都读不到它。现在：

```
$ brian evolve --payload-writers --check
checked           the page and the source agree: 24 writer(s), 4 file-level form(s), 0 named gap(s)
```

不一致时以退出码 2 结束，并逐条说明哪里不一致：

```
$ brian evolve --payload-writers --check      # 这一轮刚加完代码、表格还没动的那个瞬间
inconsistent      the page lists 22 payload writer(s) and this source has 24: the table and the calls have to move together
checked           1 problem(s): the page does not describe this source (boundary 392)
```

**规则第一次运行就拒绝了加它的那次改动**——因为这次改动新增了两个 `_write_payload(` 调用（干跑一份、真跑
一份）。这不是意外，这就是这条规则存在的意思：表格要么跟着调用走，要么页面上写的就是假话。于是表格变成
24 行，`EXPECTED_PAYLOAD_WRITERS` 也变成 24，三处（表格、测试常量、调用点）再次同数——而第四处（`--check`）
是**推导出来的**，不需要任何人记着去改。

`--check` 单独出现时被拒绝：`--check reads the --payload-writers page; without it there is nothing to check`。
检查只读源码，不碰账本（实测：在空目录里跑完，连 `var/` 都没被创建）。

**二、缺口补上，而且两边写的是同一个答案。** `--backfill-walks` 现在导出它自己的结果：

```
$ brian evolve --backfill-walks --dry-run --json-out dry.json     # 预览
{"backfilled": [], "would_backfill": [{"round": "round-a.json", "successor": "round-x.json", "at": "..."}],
 "dry_run": true}
$ brian evolve --backfill-walks --json-out real.json              # 真跑
{"backfilled": [{... "rounds": ["round-x.json"], "hand": true, "backfilled": true ...}], "would_backfill": [],
 "dry_run": false}
```

`dry_run` 是 payload 自己的一栏，因为它决定读者该看哪个列表；而"什么都没找到"时不写文件、并明说
`no payload        nothing was backfilled, so there is no walk to hand over (boundary 339).`——第 142 轮
"记录了什么都不算记录"的规则，在这里是同一个意思。

**三、真跑一次，预览在撒谎。** 到真实账本上跑预览：

```
$ brian evolve --backfill-walks --dry-run --json-out var\dry165.json
would backfill    round-20261117T201046Z.json -> round-20261118T042134Z.json (the payment is dated 2026-11-18T04:21:37Z)
... 一共 27 行
```

但真跑会写 **0** 条：那 27 笔支付第 147 轮就已经补进 store 了。预览回答的是"账本一共提名过多少笔"，真跑
回答的是"这次还会写几笔"——**两个不同的问题，一个界面**。这和"能付几轮"（第 150 轮）、"付不动的两半"
（第 151 轮）是同一类错误：计数口径必须和动作口径一样。

修法不是给预览加一个过滤器，而是让两边**问同一个函数**：`src/brian/promote.py` 新增

```python
def pending_walk_backfill(root: Path, pairs: Sequence[Any]) -> List[Any]:
    """The pairs `backfill_walks` would write: successors the store does not hold yet, one per successor."""
```

`backfill_walks` 改成遍历 `pending_walk_backfill(root, pairs)`，`--backfill-walks` 的预览也用它。于是预览与
真跑不可能再给出两个答案——它们没有各自的答案了。

**四、两种"什么都没有"要分两句。** 上面的修改一落地，真实账本上的预览立刻改口成
`nothing to backfill: no round in the ledger names the re-run that replaced it`——**这是错的**：账本明确地
提名了 27 笔，只是它们都已经在 store 里了。两种状态，两个下一步：

```
账本没提名任何支付        nothing to backfill: no round in the ledger names the re-run that replaced it
账本提名了、store 里都有了 nothing to backfill: every payment the ledger names already has a walk in the store
                          no payload        nothing was backfilled, so there is no walk to hand over (boundary 339).
```

一句"没东西可补"会同时是"没有历史"和"历史已经写完了"，而后者是好消息、前者是坏消息（第 367 轮：一张表混
了两种下一步，就得按下一步拆开）。

**边界 397**：一个页面上的声明，必须能被一条不依赖测试套件的命令检查并给出退出码；只活在 `pytest` 里的
一致性，等于对 CI 的其它检查说"这里没问题"。
**边界 398**：预览与真跑必须回答同一个问题——过滤器只加在真跑上，预览就是另一条命令。
**边界 399**：两种"没有"（没有提名 / 提名了且已完成）要两句不同的话，否则好消息和一个缺陷读起来一样。


### 3.172 闸门自己读这一页：会记着去跑的命令，不是闸门

第 165 轮让清单可以被一条命令检查（`--payload-writers --check`，退出码 2），并且它第一次运行就拒绝了加它的
那次改动。但那条命令**要有人记得去跑**：表格不再描述源码，仍然可以随一次全绿的 `--audit --strict` 进主干。
这一轮把同一个问题放进闸门——那里已经收着其它"这一版构建本身有问题"的账（第 33/81 轮的退出码 2）。

```
$ brian evolve --audit --strict        # 表格与源码一致时，闸门不多说一个字
...
audit-exit=1                           # 退出 1 来自账本欠账，与本轮无关

$ brian evolve --audit --strict        # 表格不再描述源码时（这里的 1 由检查本身给出）
payload page      1 problem(s): the --payload-writers table does not describe this source
                  run `brian evolve --payload-writers --check` for the list (boundary 397)
audit-exit=2
```

三处设计，都沿用了闸门已经有的规矩：

1. **同一份判断，两处读者。** `cmd_evolve` 在分支之前算一次 `page_problems = len(payload_writer_problems())`，
   和天花板欠账那几个计数放在一起；两个闸门（`--audit`、`--campaign`）把它传给 `_strict_exit`。闸门不自己
   重新判断，所以它和 `--check` 不可能对同一张表给出两个答案。
2. **构建缺陷不是欠账。** `page_capped` 直接是 `2`：账本里没有任何东西能把它付掉，它不是"检查了、但比政策
   更老"，而是"检查不了"（第 33 轮：2 压过 1）。它排在最后返回处，与 `broken_links` 同级——两者都是
   "这一版构建与它自己的记录互相矛盾"。
3. **报告不是检查。** 不带 `--strict` 时一句都不打印（`_strict_exit` 在 `not strict` 时提前返回 0），因为
   `--audit` 默认是报告；只有 `--strict` 把它变成检查（第 15 轮的规矩）。

**写这一轮时自己踩的坑，值得记下来**：第一版把 `page_problems=page_problems` 插在 `_strict_exit(...)` 调用
的**位置参数之间**，于是 `positional argument follows keyword argument`，`pyflakes` 立刻报，103 个测试文件
收集失败。关键字参数必须落在最后一个位置参数之后——这不是风格问题，是把 `result.inconsistent` 顶到关键字
后面去的语法错误。

**边界 400**：一条"要记得去跑"的检查不是闸门；同一个问题必须由那条已经在决定通过/不通过的命令来收费，
否则一致性只是某个人昨天的勤快。
**边界 401**：构建缺陷与欠账是两种结局——前者是"检查不了"，与谁能付它无关，一律退出 2；后者是"检查了、
超过政策"，退出 1 并且页面点名能付它的命令。


### 3.173 退出码的解释要跟着报告一起走

第 166 轮让闸门为一个不再描述源码的清单页退出 2。那次改动里有个细节当时没在意：**报告是在闸门之前写出去的**。
于是 CI 读到的是这样一个组合——

```
$ brian evolve --audit --strict --json-out var\gate167.json
payload page      1 problem(s): the --payload-writers table does not describe this source
                  run `brian evolve --payload-writers --check` for the list (boundary 397)
exit 2

$ python -c "…payload…"
['ceiling_drift', 'entries', 'gate', 'inconsistent', 'limits', …]
```

在修之前，那一栏 `gate` 并不存在：文件里每一个字段都说"没什么问题"，而退出码说"有问题"，
原因只活在那个终端里。**一个只在 stdout 上的理由，对脚本来说就是没有理由**（边界 402）。

修法很小，三处：

```
writer_problems = payload_writer_problems()
page_problems = len(writer_problems)
gate_report = {"asked": bool(getattr(args, "strict", False)),
               "page_problems": [str(item) for item in writer_problems]}
```

然后两份报告都写 `payload["gate"] = gate_report`——`--audit` 的那一栏放在 `"entries"` 之前，
`--campaign` 的那一栏紧跟着 `claim_counts`。真跑两份（同一份账本，同一张清单页）：

```
$ brian evolve --audit --strict --json-out var\gate167.json
wrote var\gate167.json
  → {"asked": true,  "page_problems": []}

$ brian evolve --campaign --json-out var\gate167c.json
wrote var\gate167c.json
  → {"asked": false, "page_problems": []}
```

`asked` 不是结论，是"有没有人要求一个结论"：`--campaign` 没有带 `--strict` 时，同一个计数不该被读成检查
通过（第 166 轮的边界 401：报告不是检查）。两条命令共用算出来的那一份 `gate_report`，所以它们不可能对同一张
清单页给出两个答案（第 160 轮"一个量只由一处算"）。

**为什么是列表而不是数字。** 第 166 轮闸门只传了 `len(...)`，因为闸门只需要知道有没有。但机器可读的那一份的
读者不是闸门——他拿到 `"page_problems": 1` 之后还得回去看终端日志，而那句话可能就是早就滚出屏幕的那一行。
所以 `page_problems` 里放**条目本身**：读者需要修的就是这些字符串（边界 403）。这也是第 161 轮那条"载荷要能
逐字重建页面句子"的自然延续——只是这次重建的是理由，而不是计数。

**边界 402**：一个非零退出码，如果它的理由只出现在 stdout，那对脚本来说就没有解释；报告里的每个判断输入
都必须跟着报告一起走。
**边界 403**：载荷里的一个计数，当被数的条目正是读者要动手修的东西时，必须连条目一起给出——只给数字等于
把读者送回日志（第 387 轮的形状：印出来的数要由载荷带着，而不是只带它的来源行）。


### 3.174 退出码就是那张清单：闸门先记账，再报账

第 167 轮让清单页的问题进了 payload，但闸门还能为另外十七件事收费，那些理由仍然只活在它打印的段落里：
脚本拿到退出码 1，知道有事，不知道是哪一件事。这一轮把闸门的判断**收成一张表**，退出码由这张表推出来，
于是"解释"和"结论"不可能再是两个读法。

```
$ brian evolve --audit --strict --json-out var\gate168.json
wrote var\gate168.json
  → {"asked": true, "page_problems": [], "exit": 1,
     "charges": [{"rule": "moved verdict", "count": 6, "code": 1}]}

$ brian evolve --campaign --json-out var\gate168c.json
wrote var\gate168c.json
  → {"asked": false, "page_problems": [], "exit": 0, "charges": []}
```

真假对照：这条真实账本上，`--audit --strict` 返回 **1**，payload 里 `exit` 是 **1**，`charges` 只有一条
——六条记录今天会被判得不一样。`--campaign` 没带 `--strict`，`asked` 是 false、清单是空的、退出码 0：
同一份账本、同一次判断，但没人要求结论（第 166 轮的边界 401）。

四个设计点：

1. **代码从清单里推，而不是并列判断两遍。** `_done()` 只做一件事：
   `2 if any(code == 2) else 1 if charges else 0`。如果哪天有人改了某条规则却不改这张表，退出码会跟着表走
   ——不会出现"表里说没事、代码说有事"（第 160 轮那条"一个量只由一处算"搬到闸门自己身上）。
2. **顺序也照文档里的等级**：构建缺陷（2）在前，欠账（1）在后，与 `_strict_exit` 的 docstring 同一份排序。
3. **短路的早退不能吞掉它没看的账。** 第一次跑自己的新测试就撞上了：`broken_links` 触发早退，于是
   `charges` 只有一条 `replacement link`，而 `moved=3` 不见了——读者会读成"只有一个缺陷"。修法是把
   **调用者已经决定**的两个计数（`moved`、`due`）在函数开头就记账，表中不再重复；早退带走它们。
   边界 405 由此而来。
4. **报告先跑闸门，再写文件。** 两个调用点从"先写 payload 再 `return _strict_exit(...)`"改成
   "先拿到 `gate_code`、把 `exit` 填进载荷、再写"。失败时仍然退出 2（导出失败就是失败），只是**页面顺序**
   变成了：报告 → 闸门的段落 → `wrote <path>`。

**为什么不是只给一个布尔。** `charges` 里的 `count` 是这道闸门当时读到的数（六条会变的记录、两条断链），
读者据此知道严重程度；`rule` 是那条规则的名字，与段落里印的是同一个词。这与第 403 轮那条"计数要连条目一起
给出"同向：机器可读的那一份要能解释它自己的结论。

**边界 404**：一份报告如果以非零码结束，它的结论必须由它可以携带的那张收费清单推出来，而不是由另一套
并列的布尔判断——否则清单与代码会各自漂移，而读者只能相信其中一个。
**边界 405**：一条短路（构建缺陷导致的早退）不能吞掉它没有检查的账；调用者已经提供的判断要最先记账，
早退带走它，否则"一个缺陷"会被读成"只有一处问题"。


### 3.175 报告与检查只差一个返回码，不差它知道的事

第 168 轮把退出码做成了一张收费清单。但那张清单**只在 `--strict` 时才算**：函数在开头就 `if not strict: return`
了，于是 `--campaign --json-out` 里永远是 `charges: []`——关于"它收了什么"这句话是真的，关于"要是当检查跑会
收什么"则一个字没有。读者想问的问题恰恰是后者。

改法只有一句：**让 `say` 替 `_print`**。

```
say = _print if strict else (lambda *a, **k: None)
```

门里 94 处打印全部换成 `say(...)`，函数体在两种模式下都跑完；`_done()` 把同一份清单存进两个字段：

```
$ brian evolve --campaign --json-out var\gate169.json      # 报告
asked False exit 0 charged 0
would
  {'rule': 'moved verdict', 'count': 6, 'code': 1}

$ brian evolve --audit --strict --json-out var\gate169s.json   # 检查
asked True exit 1
charged [{'rule': 'moved verdict', 'count': 6, 'code': 1}]
would   [{'rule': 'moved verdict', 'count': 6, 'code': 1}]
```

同一份账本、同一次读，两种模式给出**同一张表**，只有 `charges` 与退出码不同。于是"要是现在当检查跑，会不会
红"这个问题，报告自己就能回答，不必真的去跑一次检查（而检查可能要一次决策、一次导出，代价并不为零）。

三点设计：

1. **两个字段，一份计算。** `would_charge` 是"读到了什么"，`charges` 是"收了多少"。它们由同一个 `charges`
   列表在 `_done()` 里分出来，所以不可能各说各话；第 168 轮那条"代码由清单推出"因此仍然成立。
2. **打印是检查的嗓门。** 报告的页面上一个字的闸门段落都没有（`say` 是哑的），因为那些段落是为"这次检查
   失败了"而写的；一个照抄它们的报告，会让读者以为已经检查过。所以区别是**一个开关**，不是两套代码——
   两套代码会各自漂移，一个开关只会让其中一边说话。
3. **报告不为它没做的事收钱。** `exit` 仍是 0，`charges` 仍是空：`asked` 是 false。第 401 轮那条
   "报告不是检查"没有被这次改动松动。

**边界 406**：报告与检查可以在返回码上不同，不能在它们算出的事实上不同；否则"现在当检查跑会不会失败"这个
问题，除了真跑一次就没有第二个答案。
**边界 407**：两者的差别必须收在一个开关里（打印/不打印），不能收在两份代码里——两份代码会各自漂移，而漂移
的方向永远是"报告说得比检查少"。


### 3.176 报告知道的事，纸面上也要有一行

第 169 轮让报告算出了"要是当检查跑会收什么"，但只写进了 payload。终端前的人看不到：他读到一份看起来
一切正常的战役报告，而同一份账本上有六条记录今天会被判得不一样。**知道而没说，等于不知道**——这正是第
169 轮那个错误的反面。

现在报告末尾多两行（只在真要收费时印）：

```
next check        would charge 1 item(s): moved verdict (6)
                  run the same command with --strict to charge it
```

对照三种情形：

| 命令 | 页面 | payload | 退出码 |
| --- | --- | --- | --- |
| `--campaign` | 上面两行 | `charges: []`、`would_charge: [1 条]` | 0 |
| `--audit --strict` | 收费段落（多行、带解释） | `charges == would_charge` | 1 |

`--audit --strict` 时不印这两行：那一段段落已经把它说了两遍，而"再印一遍摘要"正是第 168 轮修过的
"同一件事的两个账户"。

三点设计：

1. **一行，从同一份清单来。** 规则名与计数都取自 `charges` 列表（`_done()` 里那份），所以纸面与 payload
   不可能各说各话；名字与第 168 轮收费段落里的词完全一致（`moved verdict`）。
2. **写的是"下一次会怎样"，不是"现在怎样"。** 措辞用 `would charge`，并且第二行点名那条能把条件变成结论的
   命令（`--strict`）——一个只说"会失败"不说"怎么看到失败"的条件句，读者下一步还是要猜。
3. **计数为一时不印括号。** `moved verdict` 而不是 `moved verdict (1)`：括号里的"1"没有信息量，反而让
   一行句子的常数部分变长。

**这次改动顺手修掉了两个测试的越界断言。** 第 166/169 轮有三个测试用 `assert "payload page" not in printed`
来表达"报告没有收费"——在纸面沉默的年代，这句话等价于"没收费"。现在报告会印规则名，那句话就成了
"报告不许提到自己的规则"。它们改成断言**收费段落**不在（`problem(s): the --payload-writers table`），
并保留退出码 0 的断言：否定句要否定那件事，不要否定那件事的名字。

**边界 408**：只有机器可读的那一份里存在的发现，对握终端的人来说就是不存在；反之亦然——两个读者拿到的东西
必须来自同一个算出来的值。
**边界 409**：一句条件句（"会收 N 项"）必须与它所限定的报告同页，并点名把条件变成结论的那条命令；否则读者
知道"会失败"却不知道"怎么看到失败"。


### 3.177 不收费的那几页，也得说自己没收费

第 170 轮让报告末尾印出"下一次检查会收几项"。第 171 轮要问的是：**谁在收费**。清单写的是
"`--audit` / `--campaign` 末尾带着它"，我一开始以为 `--sensitivity` 也带着——因为它附近就有一行
`_strict_exit(...)`。读到分支本身才发现：那一行属于紧随其后的 `--audit` 分支，`--sensitivity` 打印完
表格就 `return 0`，**从未走到闸门**。

这是一次很典型的误读：**按行号猜分支，不等于读分支**。那次误读还差点让我做错一件更糟的事——我先把
`--history --strict` 改成直接拒绝（`--strict` 在这页上确实什么都不收），结果一个既有测试立刻变红：
`--history --strict --max-unexplained-days 3` 是**有人用的**，那页要靠 cap 在行上标
`[past the cap: --strict exits 1]`。拒绝它等于砸掉这页存在的理由。

所以这一轮做的是：

**1. 四张"只读账本"的页都印一行、都带一个键。**

```
gate              this page reads the ledger and charges nothing; `brian evolve --campaign --strict` is the page that does
```

`--history`、`--sensitivity`、`--withheld`、`--unrecorded` 四页都印这一行，payload 里都多一个

```json
"gate": {"asked": false, "checked": false, "command": "brian evolve --campaign --strict"}
```

而 `--audit` / `--campaign` 的 payload 现在是 `{"asked": …, "checked": true, "would_charge": […], "charges": […], "exit": …}`。
`checked` 由 `_strict_exit` 里的 `_done()` 统一写入，所以"这页跑没跑闸门"是算出来的，不是各页手写的。

三张页面在这里的区别，正好是三种事实：

| payload | 含义 |
| --- | --- |
| `checked: true`、`would_charge: []` | 查了，没查出要收的 |
| `checked: false`、`command: …` | 没查（这页不查），该去那儿查 |
| 没有 `gate` 键 | 这个构建不认识"闸门"这件事 |

第三种最危险：它和第一种在脚本眼里一模一样，`payload.get("gate", {})` 两头都是空。

**2. `--history --strict` 不再沉默，但也不拒绝。**

```
--strict          this page reads the ledger and charges nothing; `brian evolve --audit --strict` turns the marks above into an exit
```

同一个 `report_footer(checked=False, asked=True)`，只换一句：读者带着 `--strict` 来这页，说明他要的是判决，
那就告诉他判决在哪一页；带上 `asked: true`，脚本能看出"他要过，这页没给"。

**3. 清单页跟着改。** `PAYLOAD_WRITERS` 里那四行本来写着"这页写出哪些键"——加了 `gate` 却不改，下一轮读
这张表的人就会以为没有它（第 170 轮刚吃过"表与源不一致"的亏）。

**边界 410**：一页没查，就必须说它没查。"没有收费"、"没查"、以及"这个构建没有这一栏"，是三件不同的事；
把它们合成一个空值，脚本与人都只能靠猜。
**边界 411**：一个开关落到一页"永远不会收费"的页面上，必须在那页上被**承认**（并指出真正判决的那条命令），
而不是被静默吞掉；只有在那一页确实什么都不读它的时候，拒绝才是对的——`--history` 上的 cap 会标行，
所以那里不能拒绝。


### 3.178 三种「什么都没有」是三件事：`--unrecorded` 的空页也要交文件

`--unrecorded` 在本轮之前，一发现"没有要重跑的轮"就直接 `return 0`——**在写 `--json-out` 之前**。
于是脚本问"现在有活吗"，得到的是"没有这个文件"；而没有文件和"跑了但失败"在脚本眼里是同一件事
（第 162 轮的形状，边界 406）。同时，三种完全不同的情况共用一句话：

```
nothing recorded: this ledger holds no round yet, so there is no file to place and nothing to re-run.
Every round that still decides something says which file it measured.
nothing to re-run: 3 round(s) name no file, and none of them still decides anything -- they are retired, or
their promise is answered.
```

三句话分别是：**账本是空的**、**每一轮都说了自己在哪个文件上**、**有轮没说文件但它们已经不决定什么了**。
读者能读出来，脚本不能——它只看到三个空列表。

**改法**：空页也写通道，并给一个字段说明这是哪种空：

```
{"rounds": 0, "unattributed": 0, "matters": [], "payable": [], "out_of_reach": [],
 "ceilings": {...}, "why_empty": "no rounds", "gate": {"checked": false, "command": ...}}
```

`why_empty` 取 `"no rounds"` / `"all placed"` / `"none deciding"`，有活时是 `null`——**null 表示"有活"，
不是"没记原因"**。真跑的两次：

```
$ brian evolve --unrecorded --json-out ..\u172empty.json      (空账本)
nothing recorded: this ledger holds no round yet, so there is no file to place and nothing to re-run.
gate              this page reads the ledger and charges nothing; `brian evolve --campaign --strict` is the page that does
wrote ..\u172empty.json
why_empty: no rounds | rounds: 0 | gate.checked: False

$ brian evolve --unrecorded --json-out ..\u172full.json       (var/demo151，2 行进不了队的)
wrote ..\u172full.json
why_empty: None matters: 2 gate: {'asked': False, 'checked': False, 'command': '...'}
```

**一页只能占一行（边界 413）**。空页要写通道，最省事的写法是再调一次 `_write_payload`，但那份清单
是按"调用行数 = 表行数"对账的：多一次调用就要多一行表，而"同一个页面写两次"这件事本身不该在清单里
出现两次。所以加了页面级的 `_write_unrecorded_payload(args, payload)`，两个分支都交给它，清单里仍是
**一行**。

**记账的计数器把自己也算进去了（边界 412）**。加完这个 helper，清单对账突然报
`the page lists 24 payload writer(s) and this source has 25`。原因不是新代码——是扫描器那一行
`if "_write_payload(" in line and ...` 里就含着自己要找的字面量，所以"24 writers"一直是
**23 次调用 + 计数器自己**。这是"没人复算的数字就是一句声明"的最短版本：它每天都在跑，每天都对自己点头。
修法是把字面量排除掉，并且把计数规则抽成一个函数 `payload_writer_calls(text)`，**命令和测试都调它**——
测试里原来各有一份复制的谓词，正是两份规则漂移的入口：

```python
def payload_writer_calls(text):
    return [line for line in text.splitlines()
            if "_write_payload(" in line and "def _write_payload" not in line
            and '"_write_payload("' not in line]
```

改完真值变了：**24 行表 = 24 次调用**（此前是 23 次调用硬凑一张 24 行的表，靠计数器补了那一行）。
新增的测试用一段人造源码证明"含引号字面量的那一行不算调用"。

**边界 412**：一个从源码里数东西的检查器，必须把自己那段文字排除在计数之外；否则它印出的数字恒高一位，
而"没人复算的数字"就是一句声明——它每跑一次都在给自己盖章。
**边界 413**：一个页面只该占清单里的一行；需要第二次写入时，给它一个页面级写入函数，而不是第二行表——
第二次调用是实现的偶然，清单记的是页面的承诺。

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

51. **第三列的 engine 指的是当前配置的判定者**：`scoring=auto` 时它读的是读者，`scoring=model` 时读的是模型。跨轮比较这三列必须同时说明配置，否则第 11 轮的 0.517 和第 22 轮的 0.833 会被当成引擎变好了。
52. **分数重复时，告警率预算控制的是计数不是比率**：读者在服务域上的分数只有几十个取值，一条 10% 的分位数会落在几十个窗口共享的值上——第 23 轮实测 14.9%。`label_free_cut` 因此把条抬到"落在其上（含）的窗口数"符合预算的位置，撑不起时返回 `None`。
53. **至多是上限，不是目标**：把 `label_free_ceiling` 当成召回目标汇报，就是第 20 轮用偷看标签的数字当承诺的错误换了个位置。上限的作用是判断某个无标签数字是否已经自相矛盾。
54. **事故单价可以靠少找事故变便宜**：`recall_at_budget` 对只动门限的候选是不变量，所以"每事件花费下降"必须同时要求在役召回不下降；否则一个把翻页量砍到五分之一的配置会被算成"更省"。第 24 轮的 `--audit` 抓到过一次。
55. **判决的名字取决于规则顺序**：同一次接受到底叫 `cost` 还是 `quality`，取决于哪条分支先命中；名字解释理由，不解释功劳大小，跨轮比较时看数不看名。

56. **战役报告是索引，不是证据。** 3.32 节那张表把 35 轮记录按流汇总，但它打印的每个数字都来自写下它的那一轮；今天真正重算的只有 audit 那一列。所以"完成 25 轮"不等于"系统变好了 x%"——要得到后者，需要同一份配置在固定留出集上跨轮重复测量，而边界 47（基线随部署移动）与边界 33（承诺依赖数据集）都不允许把历史数字直接连成趋势线。

57. **复核必须先看形状，再看结果。** 一条承诺是在某个采样、某个窗口几何上做出的；换了采样就是另一个量。先判形状不一致（`unmeasurable`），才轮到判"没兑现"（`failed`）——否则复核器会成为最危险的组件：它会用一次更严格的手续，回滚一个健康的配置。

58. **被撤回又重新成立的版本必须保留两条记录。** 只留最后一条，表格会对着正在运行的配置说"已撤回"；只留第一条，同样的表格会说"已失效"而实际在役。`checks` 列表是这类版本唯一诚实的表示：现在在役，而且曾经被正确地下线过一次。

59. **一个以窗口数计的承诺，必须连采样密度一起记录。** "最近 200 个窗口"在密度未知时不是一个时长；同一份数据换个步长重采样，200 个窗口能相差 1.4 倍的时间跨度，而复核会把那个差别当成质量退化。

60. **拒绝必须给出下一步。** `unmeasurable` 只说"判不出来"就是死胡同：它让一份承诺永远停在未知状态，而且看起来像已经处理过了。判词里要带上"怎样才判得出来"（这里是等价的窗口数），并且明确这个数字是报告而不是应用。

61. **承诺是配置与被选流共同的性质。** 3.35 节把"窗口几何"这个解释测了一遍：换到等价密度后，`v020` 从 0.297 回到 0.427，但没回到 0.507。剩下的部分是选择效应——这个窗口数当初是在那条流上测得最好才被接受的。因此**任何"这条配置能做到 X"的说法都隐含"在这条流上"**，而留出复核只能证实机制是否迁移，无法证实那个数是否会在另一条流上复现。

62. **等价换算是可比性工具，不是最优值。** 在承诺自己的流上，hint 给出的 286 比 200 更差（0.507 → 0.396）。把 hint 读成"应该改成 286"就是把一次换算当成了实验结论；它唯一的作用是让两次测量量同一个东西。

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

13. **（3.31 已完成）每次事件的花费是独立的成本出口。** `decide()` 在质量路径之前先看 `tokens_per_incident`：只要预算内在役两列召回都不降、每事件花费按成本门槛下降，就接受为 `cost`；第 19 轮那条 `reject cost` 经 `--rejudge` 当场翻成 `accept cost`（15516 → 1341 每事件）。泄漏（靠砍翻页量买便宜）由 `paging_holds` 堵住。

14. **（3.32 已完成）35 轮汇总成一张表：战役报告，以及它不能声称的东西。** `brian evolve --campaign` 从账本、版本文件与按域状态里读出每一条流的轮次数、按规则的接受/拒绝分布、在役版本与它的承诺，audit 那一列用今天的规则重算。表第一次把三件事放在同一页：46 次接受只写出 10 份版本、真正在役只有两份、而且两份都是 `unverified`；`demo` 的 13 次接受零承诺也被显式列出。剩下的路：（a）给在役版本补一次留出复核，让 `unverified` 变成 `holds` 或 `failed`；（b）让"同一配置跨轮可比"成为可能——这需要固定留出集与固定基线，而它与边界 47 直接冲突，是一个需要产品口径的取舍，不是又一条规则。

15. **（3.33 已完成）在役版本补了留出复核，并给复核加了形状前置条件。** `v019` 在 15 个没见过的 RCAEval 案例上 `holds`（0.857 对承诺 0.860）；`v020` 先被一次不同采样的复核误回滚，加 `_geometry_mismatch` 后改判 `unmeasurable`，在自己那段数据上精确 `holds`（0.507/3358 对 0.507/3358）。`--verify` 有了第三种结局：未知既不应用也不回滚，只记录。剩下的路：**让承诺自己带上采样形状**（现在 `stream` 字段记录了窗口数与事件数，但 `200` 个窗口意味着多长时间仍要靠比对才能看出），以及**给 `unmeasurable` 一个到期提醒**——一份永远判不出来的承诺与一份没复核过的承诺，区别只在于我们知道自己不知道。

16. **（3.34 已完成）把采样密度写进承诺，把"拒绝"变成数字。** `summarise()` 记录 `cadence_s`/`span_s`，`_geometry_mismatch` 多一道密度比较，`rescale_hint` 在判不出来时给出等价窗口数（200 → 286），`--campaign` 分开计数 holding / not comparable / never re-checked。实测两条 SMD 流总时长几乎相同（788.3 h 对 789.2 h）而密度差 1.49 倍，`v020` 的 0.297 因此有机械解释。剩下的路：**让 `--verify` 支持按 hint 重测**（用等价窗口数跑一次，把结果记成"同一承诺的另一形状"，而不是新版本），以及**给 `unmeasurable` 设到期**——一份永远判不出来的承诺，与一份没复核过的承诺，区别只在于我们知道自己不知道。

17. **（3.35 已完成）解释被测了一遍：窗口几何解释了 62% 的缺口。** `--verify --at-hint` 用等价窗口数重新测量被拒绝的版本，把结果记成 `verification.shape`（判决仍是 `unmeasurable`，不应用也不回滚），`format_promotions` 与 `--campaign` 会重打这一行。`v020` 在复核流上 0.297 → 0.427（承诺 0.507）：剩下的 0.080 归因于"200 这个窗口数是在承诺流上选出来的"（边界 61）；而等价值在承诺流上反而更差（0.507 → 0.396），说明它是可比性工具而非最优值（边界 62）。顺带修掉记录卫生缺陷：同一份拒绝重复复核三遍时，`checks` 不再堆出三份复印件。剩下的路：**给 `unmeasurable` 设到期**（现在一份判不出来的承诺可以无限期悬着，与"从未复核"只在记录里差一个词），以及**让提议器学会"按另一条流的密度折算"这种提出方式**——hint 能算出 286，但循环还不会主动提出它。

18. **（3.36 已完成）判词会过期，`--verify` 因此会回头再挑一次。** `verification_age_days` + `Promotion.due` + `--expiry-days`（默认 `CLAIM_EXPIRY_DAYS = 14`）把"从未复核"与"复核过期"合到同一条选择条件里，`format_promotions`/`--campaign` 打印年龄与 `expired N of them are older than X day(s)`。演示时发现并修掉第二个缺陷：不带 `--at-hint` 的普通复核会把 `shape` 写成 `null`，于是战役表上真被测出来的数字消失——现在携带旧 shape 并标 `shape_from`。边界 63（时效属于判词不属于版本）、64（复核不等于重新裁决）留下两条口径。剩下的路：**让提议器主动提出"按另一条流的密度折算"的形状**——`rescale_hint` 已经能算出 286，但循环还只会等人用 `--at-hint` 去问；以及**把到期做成一轮的输入**（到期版本自动进入下一轮候选，而不是等一次手动 `--verify`）。

19. **（3.37 已完成）提示变成提案，并且按承诺而不是按基线判。** reshape 作为候选进入每一轮（`@hintN`，带 `promise` 与 `provenance`），`decide()` 用 `reshape` 规则回答"它还能不能兑现当初的承诺"：这一轮是 **reject**——0.427 对承诺 0.507，"恢复的是可比性，不是承诺"，在役配置不动；同一批邻居按 `quality` 被接受（0.297 → 0.427），两个理由都进账本。跑这一轮抓到五个缺陷：承诺只长在候选上而没随测量走、`from_dict` 丢掉承诺、种子来自旧规则的接受、跨密度的量级被当种子、以及换个名字的同一条改动绕过了承诺规则（边界 65/66/67）。剩下的路：**把到期做成一轮的输入**（§6 第 18 条的另一条路——到期版本自动进入候选，而不是等一次手动 `--verify`），以及**把接受范围绑到采样上**（现在一次接受属于哪条流靠 `source`，属于哪种密度靠 `_same_shape` 现场比，两者都不是记录里的一等字段）。
20. **（3.38 已完成）同一几何才能相减。** `run_round` 现在按候选自己的窗口取基线（`baseline@<window>`，每个几何只测一次，写进 `references`），于是"把窗口从 200 换成 286"不再被记成 +44% 的检测收益：第 30 轮那三个"被接受的邻居"在 286 的基线下全部是平手，而 reshape 仍按承诺判为 reject（边界 68/69）。提议器同时学会不把"记录里没有本几何参照"的旧接受当种子（边界 70）。剩下的路：把几何写进 `RoundResult.baseline` 本身（`window`/`cadence_s` 与那个 `Measurement` 同层），让 `--audit` 能直接标出"这次的比较无法重建"，而不是靠推断。

21. `--audit` 的 campaign 列分成 changed / relabelled / not-reconstructable 三列，并让
    `--strict` 对"无法重建"给出语义（现在它只出现在表尾一句话里）；旧记录可以在下一次重测时
    补上几何，而不是永远停在"读不回来"。

22. 那 23 条 legacy 比较可以在下一次重测时自然消掉（重跑 round 31/32 的两轮，新记录带几何），
    而不是回填——回填等于替当时的试验补一个它没用过的参照；同时让 campaign 的 audit 列按域拆分，
    现在它是全账本一个数，而其他每一行都是按域的。

23. 让 `--retire` 能一次退役多条并把继任者写进 `--campaign`（现在每次一条，继任者要手工抄），
    以及让 campaign 显示"退役记录里的拒绝是否仍在参与 `previously_rejected`"——现在它们参与，但页面上
    看不到这一点。

24. 让 `--campaign` 的 `refusals` 也能按域拆开（现在它是账本级的），并在 `--propose` 输出里标出
    "这条候选之所以没被提出，是因为一条退役记录里的拒绝"——现在它只是静默地少了一条候选。

25. **（3.44 / 3.45 已完成）过滤器搬进生成器，省下的名额写进记录，再累加到战役报表。**
    `Proposal.refused`（`refused_skipped`）让每一轮回答"这一轮没有重复问哪些问题"；
    `Proposal.substitutes` 回答"空出的名额给了谁"；`Campaign.skipped` 把它加总成账本级的节省。

26. 让替换列表进入提案的**下一轮**：现在它只被打印，没有被用来做什么。可以把它当作"这一轮实际问到了
    多少新问题"的分母——如果一整轮的 8 条全是替换，说明提案空间的边界正在被拒绝填满，机制该换空间了，
    而不是继续在同一圈里换名字。

27. **让复现阈值按域记录，而不是全局一个数。** 当前 `_replication_loss` 用 `min_quality_gain`
    （5%）当"掉多少算掉"。异构域里一台机器的 5% 也许是可接受的权衡（它本来就低于 chance，见
    机器-2-2 的 0.037），机械地拒绝会让循环在一半的机器上永远不动。做法：把容忍度写进版本文件与
    round 记录，拒绝时同时写下"哪一台掉了多少、容忍度是多少"，让 `--audit` 能复核当年那条拒绝是否
    仍然成立。

28. **让 `--campaign` 按域统计"有多少拒绝依赖默认容忍度"。** 现在要知道一条拒绝有多依赖那个假设，
    只能手工重跑 `--audit --replication-tolerance`。做法：报表里给出"在允许一台掉 ≤X% 的条件下，
    这个域会有多少条拒绝翻成接受"，把"每台机器的容忍度"从全局假设变成可被看见的域参数。

29. **让版本承诺自己带上 bar。** 现在 `--verify` 用今天的规则复核一条承诺，而一条因 `replicate` 被挡住
    的提升从来不写入版本文件——它的 bar 只活在 round 记录里。做法：让被 replication 拒绝的候选也写一份
    "未提升"记录（含 bar 与损失），复核时按记录里的 bar 判，这样"当时为什么没上"和"现在还会不会被挡住"
    用同一个数回答。
30. **（3.49 已完成）拒绝写记录；复核按记录里的 bar 判。** 下一步：让重测一个被拒绝的候选（`--candidates`）把结果
    回写到它自己的 `w###`，并让 `--verify` 认这个家族，这样"已被拒绝"和"已复核过"就不再是两套说法。

31. **（3.50 已完成）让拒绝过期，并把复测写回拒绝记录。** 下一步：`--verify` 应当把 `withheld` 当成一个族来复核——不是等某一轮顺带重新提出候选，而是主动按域列出到期未复测的拒绝，并在必要时复测；同时 `--campaign` 的 `withheld` 行应当显示"最老的一次复测有多老"。

32. **（3.51 已完成）复核接到部署上，验过不等于发布。** 待办：`--verify-withheld` 支持一次给多个域；
`--campaign --strict` 把"最老的未复核反对"也算成一条债务。

33. **让拒绝记住自己住在哪个流里。** 这一轮的多域命令要求操作者写 `domain=stream.json`，而流是
    `w###` 记录写下时就该记住的东西：记录里有 `stream` 的摘要（源、窗口、打分窗口数），却没有这个流
    来自哪个文件的路径。把路径写进记录，`--verify-withheld` 就能不带参数地跑遍所有域 —— "记得拒绝
    存在哪里"的根源是记录自己没记住，而不是操作者记性不好。

34. **让承诺也记住自己的流。** `--verify-version` 现在同样依赖操作者写 `--stream-file`，而
    `v020` 的承诺是在 `data\smd\stream.json` 上立的、在 `stream-check.json` 上复核时被判"不可比"的
    —— 这个家族缺的是同一样东西：版本记录里没有那份文件的路径。把 `stream_path` 写进版本/承诺记录，
    让 `--verify` 不带参数就能站回承诺被立下的那条流上。

35. **（3.54 已完成）让承诺记住自己的流文件。** 第 46 轮让拒绝记住了，第 47 轮让版本记住了。剩下的是
    `--campaign` / `--sensitivity`：它们把多个域的记录放在一张表上，却仍按"数据集"分组，读者无法知道同一域里
    两个文件的结果属于哪一个 —— 下一步是让表按流文件分组（而不是按源名），或者在无法区分时明确写出"两个文件"。

36. **（3.55 已完成）按文件分组的可见性。** 页面现在会说自己的文件，但还没说**有多少比例不可归属**：
    `--campaign` 的表头应该直接给"其中 N 轮没有记录文件"，`--history` 应该每行显示文件。现在的
    `measured on … 35 round(s) without a recorded file` 已经在行里，但没有进表头，读者不从逐行读到就只能
    看到"54 轮"这个数。

37. **（3.56 已完成）把比例写进闸门，并让它自己下降。** `file unknown` 现在只是打印。下一步：给
    `--strict` 一个上限（例如不可归属比例超过 25% 就退出 1），并按域挑出仍然影响决策的轮次，在它们各自
    的文件上重跑 —— 因为补记不可能（没人记过），比例只能靠新测量降下来。

38. **（3.57 已完成）让 `--unrecorded` 直接给出重跑命令。** 现在它给出的是原则（`--stream-file <the
    file it measured>`），而"它测的是哪个文件"恰恰是缺的那条信息。下一步：用几何匹配（第 47 轮的
    `var/backfill_version_path.py` 做过一次）为每一轮列出**候选文件**，让操作员只做选择题而不是回想题。

39. `--unrecorded` 现在给出候选文件，但重跑仍要人一条条敲。让它在 `--stream-file` 与候选唯一时接受
`--rerun-unrecorded`，按候选逐个重跑、逐个记账；候选不唯一的轮次仍然跳过并说明原因（边界 104）。

40. `--rerun-unrecorded` 现在一次一轮。让它在候选文件唯一、且该轮所有候选都能从记录读回时，把 17 轮排成一张
队列并记录进度（哪几轮已重跑、还剩几轮），这样"98% 会降到多少"可以预估，而不是每次只看到一个移动的数字。

41. 让 `--rerun-unrecorded` 支持排队跑完（`--rerun-limit 0` 表示整队），并在 `--campaign` 表头给出
进度（已重跑 / 可重跑 / 剩余），这样"95% 会降到多少"在战役报表上也能读，而不是只在这条命令的输出里。

42. 让整队自己记账：`var/evolve/rerun.json` 记下每次整队何时开始、付清了几轮、停在哪里，这样
`--campaign` 能说"这笔欠账在下降"，而不只是"还剩多少"。

43. 让趋势进入闸门：`--trend-days` 选择窗口，`--strict` 在比例**上升**时失败（欠账变大是缺陷，不是一个数字），
并且让 `--campaign` 在速率第一次可测时把当时的斜率写进记录。

44. 趋势要能作为一轮的输入：`--campaign` 已经说出份额在升，下一步应让 `--propose` 先补齐欠账最大的那个域，直到它的份额开始下降；并把窗口内新增记录的文件来源逐条列出，让"上升"归因到具体的域，而不是账本整体。

45. 让"最大可付欠账"这一行也能进闸门：`--audit --max-debt-share` 在一整个域的未归属比例超过阈值时失败，
    并且让 `--rerun-unrecorded` 在按域排完序之后，把"本轮付了哪个域、还剩哪个域最大"写进记录本身。


46. **（3.65 已完成）按域的比例进闸门，队列把付款写进记录。** 剩下的一半：让 page 的 `debt` 行同时
    点名第二大欠账户，这样 `--rerun-limit 1` 连跑两次就是可见的两步；并让 `--campaign` 直接列出
    "下一步该测哪一流"的名字，而不是让读者从一张按份额排序的表里自己数。


47. **（3.66 已完成）页面点名下一条命令，并说出第二步。** 剩下的：让 `--campaign` 的 `measure next`
    在可付欠账多于一轮时给出"这一条命令会跑几轮、预计份额落到多少"，即把 `queue` 行与 `plan` 行合成
    一句可执行的预算，并在 `--json-out` 里给出同样的字段，供脚本读取而不必解析文本。


48. **（3.67 已完成）下一次调用的一句预算，以及它的 JSON 副本。** 剩下的：让 `--rerun-unrecorded`
    自己也在开头印同一句预算（现在它印 `projection` 与 `limit` 两行），把"页面推荐的命令"与"命令
    自己报的预算"合成同一个函数调用，并让 `budget` 带上 `estimated_minutes`（按每轮实测耗时的中位数）。


49. **（3.68 已完成）预算带上时间，页面与命令共用一句。** 剩下的：把 `minutes_each` 按域分开
    （SMD 与 RCAEval 的轮耗时不是一个量级），并让 `--campaign --strict --max-minutes` 能拒绝
    "这一条命令要跑二十分钟"的预算。

50. **（3.69 已完成）按域给等待定价，并让它能被 `--max-minutes` 拒绝。** 剩下的：页面还要
    给**排空**的队算一次价，让"现在全付"和"分几次付"可比；`--max-minutes` 应能按域配一个默认上限。

51. **（3.70 已完成）给"付清"单独定价，并让 `--max-minutes` 支持逐域上限。** 剩下的：把逐域上限写进
    部署状态（`var/evolve/domains/<slug>/current.json`），让它跟着域走而不是跟着命令行走；
    `--rerun-unrecorded` 自己也要在开头打印同一句预算（现在它在第一次测量后才有数字）。

52. **（3.72 已完成）上限两个值都能写，`--json-out` 也带 `limits`。** 剩下的：正文也念一句、
    上限能撤销（下一项）。

53. **（3.73 已完成）正文念出判定的上限，上限也能撤销。** 剩下的：正文里的 `limits` 句子还只有
    机器可读的那一版；下一项让"撤销"也出现在 JSON 的 `limits` 里，并让 `--audit` 报告它读到的上限。

54. **（3.74 已完成）门禁念出它读到的上限，撤销跟着它自己的命令走。** 剩下的：`--rerun-unrecorded`
    自己开头也要念同一句（现在它只念 `budget`），并让 `--audit --json-out` 的 `limits_text` 进入 CI 比较。

55. **（3.75 已完成）整队命令开头念同一句上限；`--audit` 比较"这一轮按什么上限判的"。**
    剩下的：`--campaign` 也要报同一列，并在记录足够新时把比较放进 `--strict` 的默认集合（下一项）。
56. **（3.76 已完成）页面每行报"按什么上限判的"，默认闸门只在记录够新时才亮。** 剩下的：
    `--history` 每行也报一次；`--max-limit-drift` 从"整本账"改成"只比还活着的那部分"。

57. **（3.77 已完成）`--history` 也报上限，并且默认闸门只看存活记录。** 剩下的部分：按域统计缺上限的
    轮数，让读者看见"重跑多少轮能让默认闸门亮起来"。
58. **（3.78 已完成）按域统计缺上限的轮数，并标出重跑后会写上限的轮。** 剩下的部分：`--history --json-out`
    也带上按域欠账与队列触达；当欠账可付时，让 `--campaign --strict` 能对"缺上限的比例"收费（与边界 126/135
    同形：可付才收）。
59. （3.79 已完成）把上限欠账做成可收费的一行。

62. **（3.82 已完成）** 让 `--history` 的 `ceiling debt` 行也带上命令（现在只有 `--campaign` 带），并把"排空会开出两族"
写进 `budget` 那句话里 —— 预算说的是钱，族数说的是另一个维度。

63. **（3.83 已完成）** 让 `--rerun-unrecorded` 在排队时也打印 `families` 与每一族的轮数，并把 `--without-ceilings` 的页面预算写成"本次不碰上限族"一句话，而不是沉默。

64. **（3.84 已完成）** 让 `--rerun-unrecorded` 在 `--without-ceilings` 时也打印被留下的轮数与下一句命令（现在页面有这句、命令头没有），并让 `--auto` 类批处理在拒绝一族时把拒绝写进 `queue` 记录。

65. **（3.85 已完成）** → 让 `--campaign` 从记录的 `queue.family` 汇总"已付的族"（文件族/上限族各多少轮），第三态（没写族的老记录）单独计数；"每一轮被哪一族付掉"落在 `--history` 与它的 `--json-out` 上——`--unrecorded` 列的是还欠着的轮，而付款的族属于付款者（后继记录），它不会出现在那里。

66. **（3.86 已完成）** → `--campaign` 把已付的族按 `queue.paid` 拆开（`paid by domain`，JSON 为 `queue_paid_by_domain`）；`--history` 对被队列付过的轮标出它的前身（`queue.paid_round` → `[replacing …]`），没有写前身的不补猜。

67. **（3.87 已完成）** → `--history` 的 `[replacing …]` 与 `[retired, re-measured as …]` 互相校验：`replacement_links()` 分 `mismatch` / `unretired` / `dangling` / `legacy` 四种，前三种由 `--audit --strict` 以退出码 2 计入账簿缺陷，`legacy`（第 78 轮之前写的后继无法补写）只印不收费；`--history` 标出发现涉及的两行，`--json-out` 带 `links`。

68. **（3.88 已完成）** → 链接校验的两处补齐：`replacement_links` 按"被声称的后继"分组、每个提名者单独判定，`names` 改成列表，`--history` / `--audit` / `--json-out` 都点名全部参与者；`--campaign` 页面把它已经计入 `--strict` 的 `broken_links` 印出来（历史那一端单独一行，标明"报告但不收费"）。

69. **（3.89 已完成）** → 只给 `legacy` 的一端补名（`mismatch` / `dangling` 不补：抄分歧的任一边等于选赢家）；`legacy_links()` 返回 `{retired, successor}` 对而不是去读发现的 `names` 列表；补写的端在 `queue` 里带 `backfilled`（`from` / `at` / `why` / `stream`），`paid` 与 `family` 故意为空；`--repair-links` 计划、`--repair-links --apply` 执行；`--history` / `--audit` 把补写的端单列，退出码仍为 0。`--verify` / `--promote` 拒绝在断链记录上叠加承诺的那一半留给第 70 项。

70. **（第 3.90 轮完成，2026-11-20）** 已经做出：`--campaign` 的 `links` 行把补写的端单列成 `N replacement link(s) were backfilled by a repair: their ends agree by copying, not as two witnesses`，`--json-out` 里是 `backfilled_links`；`--history` 在存在补写端时不再说"every link that has both ends agrees"。`--verify` 在循环最前面读 `provenance_block(root, version)`：断链记 `broken link` 并 **exit 2**，补写端记 `backfilled end` 并 **exit 1**，两者都不写验证记录，页面说明"每次都会再拦一次"。补写端不再被算作一次队列付款。`--promote` 的那一半留给第 71 项。

71. **（第 3.91 轮完成，2026-11-20）** 这一项的前提不成立：`brian evolve` 没有 `--promote` 入口。真正漏掉的是"没人对已经在生效的版本发问"。已经做出：`promote.in_force_promotions` 从每个部署状态的 `sources` 读出在跑哪些版本（迁移前的全局 `current.json` 记作默认域，两个变量属于同一版本只算一个），`promote.in_force_disputes` 对每个在生效版本调第 84 轮的 `provenance_block`，行里带 `domain`/`version`/`broken`/`kind`/`detail`；`Audit.in_force`、`in_force_broken`、`in_force_copied` 把它带进 `--audit` 的 `links` 段（逐行打印域名、版本、种类和细节），`--campaign` 的 `links` 段多一行 `N version(s) in force sit on a record the books dispute: v020`，`--json-out` 里是 `in_force_disputed`；`--strict` 对"在跑踩在断链上的版本"exit 2，对补写端只报告（exit 0）。真实账簿：三个在生效版本，零争议。
72. **（第 3.92 轮完成，2026-11-20）** `in_force_disputes` 每次运行都从账本重算，能说"这个在跑的版本踩在补写端上"，说不出"已经多久"。账本也给不出年龄：它记的是变了什么，不是循环什么时候第一次看见。已经做出：`promote.observe_in_force_disputes` 把每个 `(domain, version, kind)` 的 `first_seen` 写进 `var/evolve/in_force.json`（格式 `brian.inforce/1`，保留首次时间、递增 `observations`、重算 `age_days`，本轮已消失的行回传一次 `cleared` 然后忘掉；干净部署不写文件，免得长出一个暗示"曾经有过争议"的存储）；`promote.stale_in_force_disputes` 只挑**补写端**里超过 `IN_FORCE_GRACE_DAYS = 14` 天、且没有比首次看见更新的 `verification.checked_at` 的行；`Audit.in_force_stale`、`Campaign.in_force_stale` 把它带进 `--audit`（行尾 `[stale: no verdict newer than the objection]` 和一段说明，`--campaign` 印 `v020 (20.0 day(s))` 与一行"站在宽限期之外"）与 `--json-out`（`in_force_stale` / `in_force_cleared`）；`--strict` 对过期的补写端 exit 1，并点名唯一的清账命令 `brian evolve --verify --verify-version <n> --stream-file <该版本量过的文件>`。
73. **（第 3.93 轮完成，2026-11-20）** 第 86 轮记下了"第一次看见"，一行消失时回传一次 `cleared` 然后忘掉，于是页面能说"这个版本不再有争议"，说不出"**是谁清的**"。两种完全不同的事件在这里长得一样：把在跑的版本重测一次（唯一能回答补写端的动作），以及账本不再复现这条异议（观察的缺失，不是裁决）。把两者折叠，就等于让一笔债务消失并被记成已付。已经做出：存储升到 `brian.inforce/2`，多一份 `cleared` 历史，每行带 `cleared_at`、`age_days`、`observations` 和引用 `by`（`verification` 表示有一份比首次看见更新的测量；`unexplained` 表示没有），上限 `IN_FORCE_HISTORY_LIMIT = 200` 并把被裁掉的条数记进 `trimmed`；`promote.in_force_history` / `cleared_summary` 把两种清掉分开计数并给出中位年龄；`Audit.link_content` 统一了"这一页有没有链接段要说"的判断（见 §3.93 的缺陷）；`--audit` 逐行印 `cleared by …`，并单独声明"没有测量的清除不是解决，是没看见"；`--campaign` 印 `N dispute(s) left the in-force list: K cleared by a re-measurement (median … day(s)), M with no measurement on file`，`--json-out` 里是 `in_force_cleared_history`。真实账簿：零清除（没有在生效版本踩过争议），所以这一段只有在 demo 里才有内容可读。
74. ✅ 让 `--verify` 在补测成功时把"清账"直接写进存储（带 `checked_at` 与 `verdict`），而不是等下一次 `--audit` 再反推"这一行不在了"；顺带让 `--campaign` 报清账的历史（按域、只算有出处的两位小数）。**已做，但按原样做不通**：原来的措辞默认 `--verify` 能跑这些版本，而第 84 轮的护栏正是拒绝它们。落地时拆成两半——默认仍拒绝叠承诺（退出 1，点名 `--accept-copied`），加了这个开关则"测量但不落承诺"：写 verdict、写 `record_clearing`（`cited_by: measurement`）、不碰部署状态。另外加了按域拆分，以及区分"这条引用是当场写的还是事后推的"。

75. ✅ 让 `--verify` 的结论能分出"答不了"和"答了"。`record_clearing(..., tested=)` 记下这次清账有没有真的
测试承诺，`unmeasurable` 的行留在 `observed` 里、`first_seen` 不动、只挂 `untested`；`stale_in_force_disputes`
只承认 `tested` 的复核，`--campaign` 与页面各有一处单独计数，闸门在多打的整段里点名补救（换流文件，
或者退掉这个版本）。另外修了两处：补丁脚本把 `cli.py` 写成 LF 造成的 8470 行 diff，以及页面把记账短语
当成了失败原因的措辞缺陷。剩下的路见第 76 项。

76. 已完成：每条 `cleared` 带 `stream_path` 与第几次清账（`attempt`），`--verify` 结账时写进存储并
在收尾行点名文件，`--history` 印出整条链，`--campaign` 按流文件分开计数，旧条目印 `(unnamed stream)`
而不是留空。见 3.96 节与边界 191/192/193。

77. 结账条目已经"有凭据"（第 90 轮给了 `stream_path` 和 `attempt`），但没有检查那份凭据在这个 checkout
    上是否还打得开。第 91 轮加上写时检查（`record_clearing` 发现文件不存在就记成"证据缺失"的尝试，而不是
    成功结账）和读时检查（`clearing_evidence`），让 `--history` / `--campaign` / `--audit` 各自说出自己那一
    段；还发现指名一个不存在的 `--stream-file` 会让 `--verify` 直接抛 `FileNotFoundError`，改成先报错再测量。
    下一个：让 `--verify` 在指名的文件消失时用记录里的文件重测（并把这次替换写进结账条目），以及让
    `--campaign` 打印"证据缺失最久的那一笔"。

78. 已完成：`--verify` 指名的文件不存在时改用记录里的文件重新测量（并打印 `(substituted: …)`，条目里存
    `substituted_from`，`--history` 印 `[substituted for …]`；换不了的四种情形各有一句能读出下一步的回绝），
    `clearing_evidence(..., now=…)` 多带 `oldest`，`--campaign` 印"坏链接里最旧的那一条"。见 §3.98 与边界 197/198/199。
    顺带把 `var/fix_endings.py` 从手写清单改成遍历 `git ls-files`：新文件也要被约定覆盖。

79. ✅ 让 `--verify` 在指名文件不存在时改跑记录里的那份（第 92 轮），并把坏链接的年龄印出来（第 92 轮）。
    第 93 轮补上真正缺的那一半：记录**自己**指名的文件失效时没人能改，所以加了 `promote.dead_record_paths`
    （按当前检出计算，含 `in_force` 标记）与 `brian evolve --set-stream-path VERSION=FILE`（校验目标存在、
    写 `stream_path` 并把旧值记进该版本的 `stream_path_history`）。按替换次数判"记录过期"是错的对象：
    替换修的是这一次检查，不修记录，所以只作为事实统计。

80. ✅ 把 `stream_path_history` 印到 `--history`，并让 `--campaign` 统计"路径被人工修过的记录"数（第 94 轮）。
    实现为 `promote.repaired_paths(root)`：列出 `stream_path_history` 非空的版本，带整条 `moves` 链与 `in_force`；
    `--history` 输出 `stream paths` 段（`v020  <from> -> <to>  (N move(s), last <at>)`）并把结果放进
    `--json-out` 的 `stream_path_moves`；`--campaign` 输出 `links  N record(s) have a rewritten stream path …`。

81. ✅ 让 `--verify` 在开跑前说清"这条记录的位置被人工改过"，把 `stream_path_moves` 记进 `verification`，并在版本表与
    `--campaign` 上标出来（第 95 轮）。
    实现为 `Promotion.stream_path_history`（第 93 轮的字段）+ `path_moves`/`path_origin`/`path_current`；
    `--verify` 先打印 `path rewritten N time(s) by hand: <from> -> <to>`；`payload["stream_path_moves"]`
    在三条写 `verification` 的路径上都写（含定义缺失的 unmeasurable）；`format_promotions` 标
    `[measured on the repaired path]` 与 `(path rewritten N time(s) by hand)`；`repaired_paths` 增加
    `checks_on_repair`，`--campaign` 打印 `links  N check(s) were taken after the repair …`。

82. ✅ 让"原件回来了"成为可行动的事实：`repaired_paths` 增加 `origin_exists`；`--history` 在迁移行尾标
    `(the original file is back: <file>)`；`--campaign` 点名记录、文件与命令
    `brian evolve --set-stream-path v020=<原件>`；`--verify` 用原件跑时记 `on_original_path`，版本表标
    `[measured on the original path]`（与 `[measured on the repaired path]` 互斥）（第 96 轮）。


83. "把记录指回原件"是一次往返，不是又一次修复：`--set-stream-path` 指回原件时把 `stream_path_history` 里那一条
    注成一次**回归**，`why` 由命令自己写（不是调用者手写），`--history` 把"迁移 -> 回归"读成一条往返，
    `--campaign` 把"位置复原了"和"证据还没复原"分开说。**已完成（第 97 轮）**：实测见 §3.103。

84. 让 `--verify` 在一条往返记录上区分"这次判定取在原件上"和"这条承诺此前只有替代件上的证据"：
    `verification` 记录 `checked_path` 与 `prior_evidence_paths`，页面上按"取在哪个文件上"把证据分开数。
    **已完成（第 98 轮）**：`verification_evidence` / `evidence_split`，实测见 §3.104。

85. 已完成：`--verify` 先看自己的证据够不够。补丁给了 `evidence_split` 一个 `unplaced_rows`（无法定位的判断连
    时间带判词一起列出来）、给 `repaired_paths` 一个 `first_on_original`，`--verify` 在
    `on_original and prior_split["total"] and not prior_split["original"]` 时打出"这是第一次在承诺出生的
    那个文件上取判断：它持有的 N 条（判断）都量在别处"，并把 `prior_evidence_split` 与
    `first_original_measurement` 写进 `verification`；`--campaign` 把"回到原位后的第一次"和"又一次复查"分成
    两句，并把无法定位的判断按时间 + 判词逐条列出。数字上：1480 → 1492 个测试（`tests/test_first_original.py`
    10 条、`tests/test_return_trip.py` 1 条，其余是既有断言跟着新字段改），`pyflakes` 0。
    运行中抓到第二个缺陷：第 97 轮那句"它已持有的判断都取在替代文件上"在同一份记录既有一份原件侧判断时是假
    的，改成"其中 N 条取在这里、M 条在别处"（边界 217）。

86. 已完成：把"孤儿承诺"排成队列。补丁加了 `never_tested_at_origin(rows)`（没有一条判断取在承诺出生文件上的
    记录，含"一次都没查过"的那些，按出生文件分桶），`--campaign` 打出 `N record(s) hold no verdict on the file
    their promise was born with: v020 (1 verdict(s), all elsewhere) -- start with brian evolve --verify
    --verify-version 020 --stream-file <原件>`，原件不在本地时说"量不了"而不是给一条跑不通的命令；`--verify`
    在"这次检查不会碰承诺出生的地方、而那个文件还在"时补一句"量那里才是测这份承诺"（边界 218）；
    `record_clearing(..., first_original=)` 与 `--history` 的 `[first verdict at the origin]` 让"第一次在
    原件上量"在历史里也是一行。数字上：1492 → 1506 个测试（`tests/test_origin_queue.py` 12 条），`pyflakes` 0。

87. 完成：把队列变成可执行的入口（`--verify --from-queue N`），并把队列写进 `--campaign --json-out`。`promote.origin_queue`
    是唯一一份队列（`never_tested_at_origin(repaired_paths(root))["versions"]`，按版本号排序），页面、JSON 和命令都从它取；
    互斥组合（与 `--verify-withheld`、与 `--verify-version`）直接退出 2 并说明；原件不在 checkout 里的条目计数为
    `left out` 而不是丢弃；上限写成 `N of M runnable`；每条内部用**记录自己的**原件路径，而不是当前的 `stream_path`
    也不是调用者的 `--stream-file`；多条检查取最坏退出码（2 比 1 比 0）。实跑见 §3.107，`tests/test_from_queue.py` 11 条。

88. 完成：让"跑过队列"留下痕迹。`record_clearing(..., queued=)` 把来路写进条目（`True` 队列、`False` 手抄、字段缺失 =
    本轮之前写的），`cleared_summary` 分出 `queued` / `unstated_queue` 三态，`--history` 行尾加 `[from the queue]`，
    `--campaign` 印 `N clearing(s) were produced by the queue, M by a hand-typed command`（全手抄时也要印 0），
    `--from-queue 0` 只列队列不测量。实跑见 §3.108，`tests/test_queued_clearing.py` 11 条。

89. 完成（第 103 轮）：`--from-queue` 的每一次执行写进 `var/evolve/queue_runs.json`（格式
    `brian.queue/1`，保留最近 20 条并单独记被裁掉的条数），字段 `at / limit / runnable / measured / skipped /
    skipped_origins / cleared / worst / stopped_at`。`cleared` 只数这一趟自己写下的清账（执行前后各读一次观测账本），
    所以"量了三条"与"答掉了三笔"是两个数（边界 229）。`--campaign` 印出
    `the last queue run was …: measured N of M runnable promise(s) on their origins, left K out`，有清账时补一句
    `and C of those measurements left a clearing`，停住时补
    `it stopped at v020, which exited 2: a build defect is not something the next promise in the queue measures away`
    （边界 227/228）；`--campaign --json-out` 的 `queue_run` 携带同一条记录。走队列时第一条退出 2 就停，并印
    `stopped  v020 exited 2, and …`（后面还有几条就点名，没有就说没有）。另外补了一个守卫：`--from-queue` 只被
    `--verify` 分支读取，缺 `--verify` 时直接退出 2 并说明，而不是静默地改成"量一轮"。

    下一项（90）：让 `--campaign` 看队列执行的趋势（最近 N 次里几次以断账收场、平均量几条），并让 `--from-queue`
    跳过上一趟已经量过而仍然失败的那些记录（或把它们标成"量过、仍不成立"，而不是每趟都重花一次）。

90. 让 `--campaign` 读出队列走过几趟、清掉几笔、栽在账本上有几趟：`queue_history` 是窗口统计，`trimmed`
    说明有多少趟已经掉出窗口，`origins skipped on more than one walk` 把反复够不着的原件排出来。
    让 `--from-queue` 不再重复付一笔账：`blocked_versions` 记住上一趟栽在 build defect 上的记录，除非
    `--retry-blocked`，它们只被列出、不被再量；一趟什么都没量也算一趟，照样记进 `queue_runs.json`。
    （第 104 轮完成。）
91. 给每个原件文件一行档案：被账本挡过几次、最后一次是什么时候、那次是哪一条记录的书在互相矛盾，
    并让被挡住的记录在 `--history` 上点名那条矛盾的记录。
    `blocked_versions` 的每一行加上 `why`（`provenance_block` 的那句话），`blocked_dossier` 按 `first_from`
    分桶给出 `records` / `attempts` / `last_at` / `reasons`，`--campaign` 打一行
    `blocked by the books: <file> N promise(s) (v020), refused N time(s), last <at> -- <那句话>`，
    `--history` 的行只写 `[refused by the queue on <at>]`：长的矛盾句只印在它所属的那份文件上，不跟着每一行重复。
    （第 105 轮完成。）
92. （已完成）把一次拒绝和一次重试分开计数，并让修好的账本自己解掉拒绝：`attempted` 记下版本、退出码与原因；`blocked_versions` 丢掉原因已变或已消失的拒绝；`blocked_dossier` 多一份 `retried`；`--campaign` 先报"拒绝不再成立"，再报档案。文档 §3.112，测试 `tests/test_blocked_dossier.py`（12 项）。
    也可以是同一条承诺被挡三次；`attempts` 现在把两者加在一起，读者分不出这是"文件有问题"还是"记录有问题"。
    再让 `--retry-blocked` 只重试那些账本真的变过的记录（比对挡下时的 `provenance_block` 详情与今天的）。


93. （已完成）给每条被挡过的承诺一份拒绝史：`refusal_history()` 按承诺汇总"被挡几趟、第一次、最后一次、是否还成立"；`--history` 用一行 `refusals` 表头承载计数与时间戳、行上只留短标记；`--campaign` 与 `--json-out` 用同一份数字。文档 §3.113，测试 `tests/test_refusal_history.py`（10 项）。

94. 已完成：拒绝史用趟号说话（第 108 轮）。`refusal_history()` 的每一行现在带 `first_walk`/`last_walk`（按时间
    排序后在窗口里的序号），`latest_refusals()` 单列"最后一趟拒了谁"，`blocked_dossier()` 为每个原始文件记上
    `refused`（这个文件名下的承诺合起来被拒了几次）与 `walks`（窗口多大）。`--history` 打两行（版本自己的拒绝
    史 + 最后一趟的结论），`--campaign` 的档案行末尾写
    `(walk 2 of 2; its promises were refused 2 time(s) in the recorded walks)`。1587 → 1597 tests。
95. 已完成：拒绝史与档案互相指认（第 109 轮）。`refusal_history()` 的行多一个 `refused_walks`（被拒过的趟号
    列表，不只是首末两趟），`blocked_dossier()` 的每个原始文件条目把它名下各承诺的趟号合起来，`walks_phrase()`
    把 `[1, 3]` 写成"walks 1 and 3"；`--campaign` 的档案行改成
    `(walk 2 of 3; its promises were refused in walks 1 and 2, 2 time(s))`，`--from-queue 0` 的两处提示（`still
    blocked` 与清单行）都带上"几次、哪几趟、是否还成立"。1597 → 1606 tests。
96. 已完成：把"要不要花一轮修账本"变成可执行的判断（第 110 轮）。`repair_plan()` 为每个被挡的原始文件算一份
    代价——`walks_blocked`（花了多少趟被拒）× `max(1, held)`（名下还排着几条没在原件上量过的承诺），并给出
    `remedy`；排序按代价、再按趟数、最后按文件名（两次运行不能给出两个顺序）。`--verify --from-queue 0` 末尾打
    `repair order`，"没有可量的承诺"分支也打；`--campaign` 打 `repair next:`，`--campaign --json-out` 的
    `repair_plan` 让脚本不必解析句子。1606 → 1617 tests。
97. 已完成：让修复本身留下账（第 111 轮）。`--repair-links` 与 `--set-stream-path` 在动手前后各取一次
    `repair_plan()`，把 `before` / `after` / `freed` / `cost_fell` 写进 `var/evolve/repairs.json`，
    `--campaign` 打"最后一个修复把哪个文件推出了表、代价从 X 降到 Y"，`--campaign --json-out` 给出
    `repair_history`。这一轮的第一个实测结论是反面的：两条修复命令改的都是"另一本书"，清不掉正在拒绝的那本，
    所以每一次可执行的修复都只说"什么也没离开这张表"——账本拒绝只有诊断（`--audit`），没有可执行的修法。98. **已完成（第 112 轮）**。`brian evolve --settle-link RECORD=SUCCESSOR [--settle-why TEXT]` 把"哪一条声称已经过时"记成一条决定：`var/evolve/settlements.json` 存 `{record, successor, at, why}`，重复执行幂等并说出原时间；`replacement_links(rounds, settled)` 只让被裁定的那一个声称退出候选，**不改账本**（`queue.paid_round` 原样保留）；`--audit` 把结算单独打印（"not by agreement…it is not a second witness"），`--strict` 因此从 2 回到 0；`provenance_block` 读同一份结算，`--verify` 不再拒绝叠承诺；`--history` 用同一份结算读（`every replacement link that no decision settled agrees…`、`[settled with … by a recorded decision]`），`--campaign` 页面与 `--json-out` 都带 `settlements`；`repair_plan` 的 `remedy` 改为 `brian evolve --settle-link …`，第 111 轮那句"changed nothing in the plan"第一次能打印出"1 file(s) off the table"。实测：`--audit --strict` 2 → 0，`repair plan      1 file(s) off the table: data/smd/stream-real.json -- cost 1 -> 0`，campaign JSON `settlements` 带理由与时间。测试 1617 → 1652（`tests/test_settle_link.py` 17，新）。
99. **已完成（第 113 轮）**。`--settle-why` 对新决定必填（`settle_link` 抛 `a settlement needs a reason…`，CLI 退出 2 且不写文件；已成对的重复执行仍幂等）；新增 `--settle-by NAME` 把裁定者写进 `by` 字段，不给就存空、并由 `--audit` 明确说 `and no decider was recorded` / `N of them were recorded without --settle-by`，`--campaign` 则按裁定者计数（`decided by: alice 1` / `decided by: and no decider recorded 1`）；第 112 轮留下的"有理由无裁定者"记录被如实标出；第 112 轮之前允许的"无理由"记录也被点名，但本构建只能报告、不能补写。演示：`--settle-by alice` → `decided by: alice` + `repair plan 1 file(s) off the table … cost 1 -> 0`；新对不给理由 → 退出 2 且 store 不变；`--audit` 对旧记录打印 `and no decider was recorded`。测试 1653 → 1664（`tests/test_settle_link.py` 28）。

100. 给已经有记录的决定补上理由与裁定者：`--settle-note RECORD=SUCCESSOR --settle-why … --settle-by …`
      只追加一条 note（`notes: [{at, why, by}]`），不改原有的 `at`/`successor`/`why`；补写这件事本身记进
      `repair_history`（action 为 `settle-note`，计划没动也照实打印）；`--audit` 把"补写的说明"排在
      "决定自带的理由"之后并单独成段，`--campaign` 单独数"事后才补上理由的决定"。第 114 轮做完了这件事
      （1664 → 1677 个测试）。留下的问题见第 101 项。
101. 让 `--campaign` 把"事后补写的说明"按人拆开（`explained later by: alice 2, and no explainer named 1`），
     `--history` 的结算行带上 note 的条数与最后一位说明者，`--settle-link` 因缺理由被拒时两步都说清出路：
     错误文本里点名 `--settle-note`，被拒绝时再按现存记录数打印一行 `hint`。第 115 轮做完了这件
     （1677 → 1687 个测试）。留下的问题见第 102 项。
102. 三件事一起收口：`settlement_summary(settlements)` 成为唯一口径（pairs / explained / explained_later /
      unexplained / deciders / explainers / unattributed），`--audit --json-out["settlements"]` 与
      `--campaign --json-out["settlement_summary"]` 都读它；同一作者在同一秒里写两条不同理由被拒（存储按时间排序，
      排不出先后的东西不许当成序列列出来）；`--history` 的行上标出 `[settled with X and no reason was recorded]`。
      第 116 轮做完了这件事（1687 → 1699 个测试）。留下的问题见第 103 项。
103. 让"没人能解释的决定"也能被闸门收费：现在 `settlement_summary["unexplained"]` 只出现在页面上与 JSON 里，
      `--strict` 永远为它返回 0；加 `--max-unexplained-days N`（默认不启用），一条没有理由、没有 note、且已经存在
      超过 N 天的裁定进闸门，并在 `--history` 里点名是哪几条；同时让 `--campaign --json-out` 的每行带上它对应的
      裁定状态，脚本才能算出"这个 stream 上有几条决定没人能解释"。
     （第 117 轮完成：`unexplained_decisions(settlements, now)`；`--max-unexplained-days DAYS`（默认关，只对 `--strict` 生效，不带 `--strict` 直接退 2）；`--audit`／`--campaign` 两个闸门调用点都传 `unexplained_stale`／`unexplained_days`；`--history` 印 `decisions` 一段并给过期行加 `[past the cap: --strict exits 1]`；`CampaignRow.settlement_state` 进 `--campaign --json-out` 的每一行；`tests/test_unexplained_gate.py` 11 个测试；边界 266/267/268。）
104. **已完成**：让「证据后来又动了」变成一条单独的债。`moved_settlements(rounds, settlements)` 对每条决定检查三个事实——被裁定的记录是否还在账簿里、它的退休标记是否还在、它是否还指向同一个后继——再加上「决定之后才到达的、有自己时间戳的同一后继主张」。`--audit` 打印「其中 N 条所依据的账簿在决定之后已经动过」，`--history` 打印同样的行，`--campaign` 把每行流按 `moved` 单独计数并导出 `settlements_moved`。**不收费**：本版本里没有任何命令能重开一条决定（边界 271），所以 `--audit --strict` 对「证据动了」仍然是 0。测试 `tests/test_settlement_evidence.py`（11 条）。
    - 第 112 轮的前提被这一轮纠正了一次：第一版规则把「任何另一个主张者」都算作证据动了，结果每一次对 `mismatch` 的裁定当场就是陈旧的（11 条测试里 7 条挂在夹子上）——第 112 轮只消掉**一个**主张者的主张，另一个端点是刻意留着的。规则改成「**晚于**决定的、有日期的主张」。
    - 第二次纠正：主张的日期只能取退休标记自己的 `at`。`finished_at` 是那一轮**跑完**的时间，不是退休被写下的时间（第 83/93 轮都是事后补写的），拿它当日期会让一条几十天前就摆在那里的主张看起来「刚刚到达」（边界 272）。
105. **已完成**：让一条决定可以被重开。`--settle-reaffirm` 保留第一天的日期、追加 `{at, why, by, claimants}`，`_effective_moment` 把「决定现在说话的时刻」推进到最新一次确认，`moved_settlements` 据此判断主张是否「后来才到」；`--max-moved N`（默认关）只对 `moved_clearable` 的那一类收费，`--history` 的行上并排打印 `[re-taken N time(s), last on …]` 与 `[never re-taken: …]`。测试 `tests/test_settlement_reaffirm.py`（15 条）。
    - 自己的缺陷：`clearable` 起初用子串 `"successor" in reason` 判断，而「后来的主张」那句含 successor，于是每一行都不可清掉——本轮唯一要收费的类别被自己的判断排除。改成布尔 `pair_gone`。
    - 文档缺陷：第 118 轮说「追加第 105 项」，而文件里没有第 105 项——只在末尾追加的队列，末尾是它唯一的锚点。
106. 已完成（第 120 轮）：`--campaign` 现在按来源文件给出「几条决定被重开过、几条仍然陈旧」（`reaffirm_plan`，含一条可直接运行的命令），`--settle-reaffirm` 接受多个 `RECORD=SUCCESSOR`（一条理由、一条 `--settle-by`、被拒的一对不中止其余），`--json-out` 的形状不再随参数个数变化。

107. 已完成：重开的决定在 `--campaign` 的 JSON 里带整条时间线（`last_retaken` / `walks_since`，未知印成 `unknown`），并新增 `--max-moved-per-file N`，让「这个文件的账还没人读过」按文件计价；边界 279/280/281。

108. ✓（第 121 轮）计划要带时间线，欠账要能按文件收费：`reaffirm_plan` 现在带 `last_retaken` 与 `walks_since`（没人算过的数印 `unknown`，不是 `0`），`--max-moved-per-file N` 按「一个文件上有几条决定」收费，和按条数的 `--max-moved` 并排各自给出自己的判决。

109. ✓（第 123 轮，改写了原意）原本排的是「只重开 `last_retaken` 之后变脏的那些」，但 `reaffirm_plan` 里列出的每一条**已经**是「上次确认之后变脏」的那一类——再按时间过滤等于什么都不选。真正缺的是两件事：把 `never` / `again` 两种活分开付（`--settle-kind`，并在输出里说留下了什么），以及把两栏按域分进 `--campaign --json-out` 的行里。

110. ✓（第 124 轮）重开的时间线：`retake_timeline` 给出每条决定的 `taken` / `retaken` 有序条目（含署名与每次覆盖的主张），`frequent_retakes` 点名被重开三次以上的决定，`settlement_summary` 多一栏 `repeated`，两条页面（`--campaign` 的 `links` 行与 `--history` 的行标注）都从这份时间线读。


111. （第 125 轮已做）给被撤销的决定一条出路——现在有了：`--settle-drop RECORD=SUCCESSOR --settle-why TEXT
     [--settle-by NAME]` 把 `{at, why, by}` 追加进这条决定自己的 `withdrawn`，保留它的日期与后继；`replacement_links`、
     `provenance_block`、`blocked_pairs`、`settlement_summary`、`unexplained_decisions`、`frequent_retakes` 都只读还站着
     的决定，于是被它结算掉的矛盾重新成为矛盾（`--strict` 变严而不是变松），而它自己不再欠任何解释或重开。顺带修了三处：
     `repair_plan` 的 remedy 不再指名会拒绝自己的 `--settle-link`，撤销块从 `if result.settled:` 里搬出来，
     `Audit.settled` 改为只放站着的决定。

112. （第 126 轮已做）给被撤销的那一对一条「重新决定」的命令——现在有了：`--settle-restore RECORD=SUCCESSOR
     --settle-why TEXT [--settle-by NAME]` 往这条决定自己的 `redecisions` 里追加 `{at, why, by}`，不动 `withdrawn`；
     `is_withdrawn` 改为「最新的撤销是否晚于最新的重新决定」，于是 `replacement_links`、`provenance_block`、
     `blocked_pairs`、`settlement_summary`、`unexplained_decisions`、`frequent_retakes` 一字不改地跟着对。`--audit`
     单开一块、`--campaign` 单开一行、`--history` 行上写两个时刻，`settlement_summary` 多一个 `redecided`。顺带修了四处：
     `_settle_command` 与 `settle_link` 的判词改为指名 `--settle-restore`（不再说「没有这个命令」），
     `restore_settlement` 的幂等检查提前到拒绝之前，历史行上的标记只落在被决定的那个记录上（第 124 轮的同一处），
     以及 CLI 夹具的时钟。
113. ✅ 给「撤了、一直没人重新决定」的一对收费（本轮）：`unanswered_withdrawals()`、`--max-withdrawn-days N`（默认关，`--strict` 才读）、`--audit` / `--history` / `--campaign` 各印年龄，`--settle-restore` 清账。演练发现「年龄收费只在矛盾已经和好时才有意义」，写进边界 298。

114. **已完成**（第 128 轮）：`withdrawal_plan(rounds, settlements, now)` 按记录自己的 `stream_path` 分桶，
     每桶给出条数、最老一条的年龄与 `--settle-restore RECORD=SUCCESSOR --settle-why TEXT` 这条命令，桶按
     「条数降序、文件名」排；`--campaign` 打印 `withdrawn and waiting, by stream file: …`；`--audit --strict` 加
     `--max-withdrawn-per-file N`，只在一个文件的条数超限时收费，并把文件名与边界 279/299 说出来；`--history` 的
     撤销块与 `--campaign --json-out`（`withdrawal_plan`）读同一个函数。边界 299、300。

115. **已完成**（第 129 轮）：`--settle-restore-file STREAM` 从 `withdrawal_plan` 里取出该文件的全部待办对，
     一次交回给 `--settle-restore` 那条（现在是 `nargs="+"`）分支，所以逐对输出、被拒一对不打断其余、`--json-out`
     列表这些行为全部继承；计划里的 `batch` 改成付整个文件的命令，只留一个待办时才回落到单对命令。边界 301、302、303。

116. **已完成**（第 130 轮）：`--dry-run` 只对 `--settle-reaffirm-file` / `--settle-restore-file` 生效：列出它会动的
     每一对（含种类、撤销时间与天数），一行也不写；作用于其它命令时直接 exit 2 而不是被忽略。顺带修掉第 129 轮留下的一处
     不对称：`--settle-restore-file` 的 `--json-out` 被显式清空，批量付了 N 条却不留 payload。边界 304、305、306。

117. **已完成**（第 131 轮）：干跑在列出要动的对之后，再用一份**没有写下去的副本**算一次事后状态——
     `after this run  <文件> would owe 0 unanswered withdrawal(s), and the ledger would go from 3 to 1`。能算的是
     `withdrawal_plan` / `reaffirm_plan` 这两个纯函数于 settlements 列表的取值；不能算的（修复账单读的是队列里存下的
     拒绝句子，不在 settlements 里）印一句说明它没被预览。边界 307、308。

118. **已完成**（第 132 轮）：`--campaign --what-if-records <file>` 在页面之后打印该文件的两个问题——要付的撤销、
     要重开的决定，各自一行事后账（`would owe N …, and the ledger would go from A to B`），并注明「这里没有写任何东西，
     这是干跑同一份推算」。作用于没有 `--campaign` 的命令行时 exit 2。边界 309、310。

119. 已做成：`reaffirm_plan` 把不可付的对分离成 `unpayable`/`unpayable_pairs`/`stuck`，带上 `--settle-drop` 补救；`--campaign` 单独一行点名（文件、当前后继、命令）；批量命令的头行数字与之自动一致；`--max-moved-per-file` 只数可付的；顺带修掉 `moved_settlements` 把撤销过的决定算成「欠一次重开」的旧账。1902 tests，pyflakes 0。
120. 已做成：`--settle-drop` 改成 `nargs="+"`（载荷为列表，含 `drop_refused`/`drop_skipped`）；新增 `--settle-drop-file STREAM`，对子取自 `reaffirm_plan` 的 `unpayable_pairs`，走单对分支、按文件一次付完；`--history` 的 settled 行新增 `[cannot be re-taken: the record names <后继>]`，与 `[retired, re-measured as …]` 对得上。1911 tests，pyflakes 0。
121. ~~把 `--dry-run` 教给第三条批量命令（`--settle-drop-file`），并让 `--campaign --what-if-records` 把"不能重开"的那一族也算进"付完还剩什么"。~~ 已完成：预览说清"这条决定会搬到没人接手的队列里"，页面对三个家族各算各的，共用 `_after_preview_lines()`（§3.141，边界 318/319/320）。

122. ~~让预览能回答"如果我按这个顺序做"：`--campaign --what-if-records FILE --plan restore,drop,retake` 依次算出每一步之后的两张账。~~ 已完成：每步在 store 拷贝上跑一条真命令，最后按反序再算一遍，只在结果不同时说"顺序有影响"（§3.142，边界 321/322/323）。

123. 已实现：`restore_settlement` 与 `reaffirm_link` 一样拒绝记录已经不指向的后继，拒绝语给出
     `--settle-link` 与 `--settle-drop` 两条出路；`withdrawal_plan` 把撤回队列分成 `records`（能付）与
     `unpayable`（付不掉），行里多了 `waiting`（总数）、`unpayable_pairs`、`stuck`；`--settle-restore-file`
     先付能付的、再报剩下的，只剩付不掉时退出 2；`--campaign`、按文件队列行、`--what-if-records` 都按
     可付/总数两个数说话。见 §3.143。

124. 已实现（第 138 轮）：`--settle-reanchor RECORD --settle-why TEXT --settle-by NAME` 把一条裁定的目标改到记录如今
     指的那一支（`successor_history` 记下 `from/to/at/why/by`，`at` 不动，store 之外一个字节都不改），
     `--campaign` 单列"被重新锚定的裁定"，`--history` 在那一行标 `[re-anchored on …: old -> new]`，
     `settlement_summary["reanchored"]` 计数。原文请的 `--settle-link --repoint` 没有做：那条命令写的是"新裁定"，
     把"旧裁定改锚"塞进同一个入口会让两种语义共用一个开关（第 226 轮边界的形状）。
125. 已实现（第 139 轮）：`reanchor_plan` 按记录的文件分组（只收还站着的裁定），`--settle-reanchor-file STREAM` 从清单取记录、
      `--dry-run` 先印每一对与"做完还剩几个可付、几个仍指向旧目标"，真跑递归进单条那一段（拒绝语/JSON 都只有一份实现）；
      `--settle-reanchor` 改成多条，一条理由覆盖整批，被拒的一条不停整批、整批退出 2；`--campaign` 多一行
      `N decision(s) still name a successor their record has left: … -- <batch>`，`Campaign.reanchorable` 进 `--json-out`；
      `--campaign --what-if-records` 把改锚作为第四族算进去，"没有可付的了"必须四族都说没有。见 §3.145。
126. 已实现（第 140 轮）：`--settle-drop-file` 与 `--settle-reanchor-file` 不再把 `inner.json_out` 置空，批量写的就是
      单条那段写的那个载荷（`{"reanchored": [...]}` / `{"withdrawn": [...], "drop_refused", "drop_skipped"}`），
      四个文件命令因此同形；四条 dry-run 分支各印 `no payload      a preview writes nothing …`；一条都没付的批量
      退出 2 且不写载荷。见 §3.146。
127. 已实现（第 141 轮）：八个写 `--json-out` 载荷的点收进一个 `_write_payload(path, payload) -> bool`——
      建父目录、写 `<target>.tmp` 再 `replace` 落位、印 `wrote <path>`；写不出来时印
      `cannot write the payload to …` + `what this command recorded is already in the store: only the payload is
      missing` 并退 2，而不是在账已经改完之后抛回溯。`--repair-links` 那处的 `__import__("json")` 一并清掉。见 §3.147。
128.       剩下的静默点，为的是不互相覆盖），于是一整趟队列跑完，读者拿不到"这一趟测了哪几个版本、各是什么结论"。
      要么按记录写进一个目录（`--json-out DIR`），要么在文件里追加一个 `{"walks": [...]}`。

129.       并让 `--from-queue` 的载荷带一个 `store` 字段说明它是仓库里的第几条记录——今天读者要靠时间戳才能
      把载荷和 `var/evolve/queue_runs.json` 对上。

130. （已完成）让每一轮记下"它是第几次行走产生的"，`queue.walk` 指回重跑队列自己的仓库
    （`var/evolve/rerun_runs.json`）；`--history` 的行因此能说 `walk N`。见 §3.150。
131. ~~让 `--campaign` 读重跑队列的行走（几次、几次空手而归、最后一次），并让 `--history` 在号被裁掉时说
     "它指的行走已经不在仓库里"。~~ 已完成（§3.151）：`rerun_walks()` / `format_rerun_walks()` /
     `walk_held()`，号改成绝对号并写进条目，`--history` 印 `walk N, no longer in the store`；
     真账本上 74 轮、零次队列行走。
132. ~~让人手付的那一轮也在重跑仓库里留下一条行走（今天 `queue.walk = 0` 既表示"不是队列付的"，也表示
     "没记"），`--campaign` 因此能说"这台机器上的重跑，队列占到了几成"。~~ 已完成（§3.152）：条目带
     `hand`，页面分成"队列几次 / 人手几次"，没有后继不记、队列已付不重复记。
133. ~~把真账本里那 74 份记录的退休补记成"人手行走"，好让 `--campaign` 的"队列占几成"覆盖整本账；补记必须
     自报（`backfilled`），不能冒充当时记下的。~~ 已完成（§3.153）：`--backfill-walks`（含 `--dry-run`）读
     `retired.re_measured_as`，去重后写出 17 笔，条目带 `hand`/`backfilled` 与付款本身的时刻；页面分两半印并
     说明"事后补记"。真账本结论：队列付了 0 笔。
134. ~~把 `--dry-run` 的家族清单收敛成一张表，句子与判断都从它生成。~~ 已完成（§3.154）：`BATCH_PREVIEWS`
     一张表供判断、拒绝句子与帮助文本三种读法；结构测试从解析器读出 `--settle-*-file` 家族加例外
     `--backfill-walks`，逐项比对；`var/prove_148.py` 逐行删除以证明测试会触发。2050 tests。
135. ~~让 `--campaign` 的"重跑行走"那行带上能改变它的命令。~~ 已完成（§3.155）：新的 `re-run offer` 行给出
     今天可付的轮数（来自守卫同款的两份名单）、该敲的那条命令，以及"这个仓库的 17 笔全是人打的"；同一份
     结构进 `--campaign --json-out` 的 `rerun_offer`；命令字符串收进 `_rerun_command()`，与 `ceiling debt` 那行
     由测试比对。真账本：9 轮可付。2050 → 2062 tests。
136. ~~让 `--rerun-unrecorded` 在开始测量前，把同一句 offer 印出来（今天能付几轮、会不会带上 `--with-ceilings`），
     这样"页面说的"和"命令做的"是同一句话；命令结束时再印一次剩下的（第 60 轮 `budget_text` 的形状：一处算、
     两处印）。~~ 已完成（§3.156）：`offer` 与 `left to pay` 两行都由 `_rerun_offer()` 生成；顺手修掉了第 149 轮
     的分母——"能付"只数恰好一个候选文件的轮，说不清文件的单独计数（`unplaceable`），真账本第一趟队列付款
     （walk 18，9 轮）就是这次拖干的产出。
137. ~~让 `--campaign` 的上限欠账行也说清 47 轮里有多少轮是"任何命令都付不动"的，并把 `--unrecorded` 的行
     分成"可付"与"付不动"两段。~~（第 151 轮：§3.157）

138. ~~让 `--unrecorded` 的"付不动"那一段也能被修好：写一条命令把"没有单一文件"的轮补上文件，跑完之后这一段
     自己变短。~~（第 152 轮：§3.158）

139. **（已完成，见 §3.159）** 让"补回去的文件"也能被反过来检查：`--claim-round-file` 写下的
     `geometry: disagrees` 是一条人的断言，重测之前和页面上都要说出来，并让 `--campaign` 数出有几条记录
     站在这类文件上。
140. **已完成（见 §3.160）** 一次重测现在是"踩在谁的断言上"完成的：`RoundResult` 多了 `stream_path_history`，
     队列重跑把被继承的条目（`inherited_from`、原来那条 `why`、新记录自己的 `at`）写进它写出的新记录，
     于是 `claim_note`、`claimed_rounds` 与 `--campaign` 在新记录上仍然数得到那条断言。
141. **已完成（见 §3.161）** 被继承的断言带上了自己的年龄：`claim_origin`（最初记下它的轮）、
     `claim_since`（那一刻，不随重测刷新）、`claim_hops`（继承了几次）；`claimed_rounds` 与 `--campaign` 说得出
     "这条断言被继承过几次、最初记在哪一轮"。
142. **已完成（见 §3.162）** `--backfill-claims` 给第 154 轮写出的继承记录补上 `claim_origin` /
     `claim_since` / `claim_hops`：能顺着 `inherited_from` 推导出断言时刻的就推导，走不到就写
     `claim_since_source: "unknown"` 而不是拿重测时刻顶替；每条被补的条目自报 `backfilled`。
143. **已完成（见 §3.163）** 修补出来的年龄带上它的来历：`claim_since_source`（`derived` / `unknown` /
     `backfilled`）随继承一起传；`claimed_rounds` 的行与 `--campaign` 的年龄句都会为 `backfilled` 的那一条加一句
     "这个日期是修补补出来的，不是记录自己带着的"，两跳之后仍然在。
144. **已完成（见 §3.164）** `--history` 的行尾标出"这一轮站在一条人的断言上"，以及继承了几次、
     年龄是不是补出来的（`claim_mark`）；`agrees` 不标。三行 demo 分别是 0 次继承、1 次、2 次且年龄被补过。
145. **已完成（见 §3.165）** `--history` 头行数出"N of M 行站在一条人的断言上（K 至少继承过一次、
     J 条条目的 claim 栏是修补写的）"，并把退休进后继的那些单独数一行；没有断言时整行不出现。
146. **已完成（见 §3.166）** 计数搬进 `campaign.claim_counts(rounds, population=...)`，返回
     `ClaimCounts`；`--campaign` 与 `--history` 都印 `sentence()`，各自用 `population` 报名（`live round(s)` /
     `row(s)`）。测试钉住"两页那句话只差总体名"。
147. **已完成（见 §3.167）** `--campaign --json-out` 与 `--history --json-out` 都带 `claim_counts`
     （`ClaimCounts.to_dict()`），并且测试用 payload 重建页面那句话：`ClaimCounts(**payload).sentence()` 必须
     等于印出来的那行。
148. **已完成（见 §3.168）** `--history`/`--campaign` 的 payload 也走 `_write_payload`（`wrote` 只印一次），
     并给写手加了 `recorded` 参数：报告页写不成时说"什么都没记录，只丢了导出"，不再套用结算命令那句。
149. **已完成（见 §3.169）** 盘点结果：8 处自己写盘的 payload 全部改走 `_write_payload`（`recorded` 区分
     改库与报告），现在是 0 处裸写、22 处调用点；`tests/test_payload_inventory.py` 把这两个数字钉成常量，
     并禁止源码里再出现 `write_text(_json` / `Path(args.json_out).write_text`。
150. ~~让"导出清单"能印出来：`brian evolve --payload-writers` 说明每条命令导出什么、失败时用哪一句。~~
     第 164 轮完成（§3.170）：`PAYLOAD_WRITERS` 22 行 + `format_payload_writers()`，与测试里的计数、源码里的
     调用点三处同数；顺带修正 `--claim-round-file`/`--backfill-claims` 在 `--dry-run` 下的 `recorded=`，
     并把"`--backfill-walks` 根本没有 `--json-out`"这个缺口印在页面上。
151. ~~给 `--backfill-walks` 补上导出，并让 `--payload-writers --check` 在表格与源码不一致时退出 2。~~
     第 165 轮完成（§3.171）：`--check` 第一次运行就拒绝了加它的那次改动（22 行表 vs 24 个调用），缺口补上后
     表格 24 行、`EXPECTED_PAYLOAD_WRITERS` 24、`--check` 推导验证；顺带发现预览在真实账本上承诺 27 笔、
     真跑写 0 笔（补进去的都已存在），改成两边共用 `pending_walk_backfill()`，并把"没有提名"与"提名了且
     已完成"分成两句。
152. ~~让闸门自己读这一页：`--audit --strict` / `--campaign --strict` 在 `payload_writer_problems()` 非空时
     以退出码 2 结束。~~ 第 166 轮完成（§3.172）：`page_problems` 算一次、两个闸门共用，与 `broken_links`
     同级归入"构建缺陷"（退出 2）；不带 `--strict` 时一字不印（报告不是检查）。
153. ~~让退出码在机器可读的那一份里也有解释：两份报告的 payload 都带 `gate`（`asked` + `page_problems`
     条目本身）。~~ 第 167 轮完成（§3.173）：报告在闸门之前写出，所以理由必须跟着报告走，否则
     `--audit --strict` 退出 2 而文件里每个字段都说没事（边界 402）。
154. ~~让 `gate` 说出闸门读过的每一个计数：`--strict` 下 `_strict_exit` 把每次收费记进清单，退出码由清单
     推出，两份报告的 payload 都带 `charges` 与 `exit`。~~ 第 168 轮完成（§3.174）：17 条规则收成一张表，
     调用者已知的 `moved`/`due` 先记账（短路早退不再吞掉它们），页面顺序改为"报告 → 闸门 → 写出"
     （边界 404/405）。
155. ~~让报告也能回答"要是现在当检查跑，会失败吗"：非严格模式下算同一张表，写进 `gate["would_charge"]`
     （`charges` 仍为空、退出码仍为 0）。~~ 第 169 轮完成（§3.175）：94 处打印换成 `say`，两种模式读同一份
     事实，区别只在 `charges`/退出码（边界 406/407）。
156. ~~让页面也说出"下一次检查会收 N 项"。~~ 第 170 轮完成（§3.176）：报告末尾印
     `next check        would charge N item(s): <rule (count)>` 与具名命令，`--strict` 时不印（那一段段落
     已经说过），规则名与计数取自同一份 `charges` 清单（边界 408/409）。
157. ~~让这条摘要也出现在 `--history` 与 `--sensitivity` 上，并把"报告收尾"收成一处。~~ 第 171 轮完成
     （§3.177）：清单页说的范围本来是对的，我先猜错了（把 `--audit` 分支的 `_strict_exit` 读成了
     `--sensitivity` 的）。现在四张只读账本的页（`--history`、`--sensitivity`、`--withheld`、
     `--unrecorded`）都印同一行、payload 都带 `gate`，`checked` 由 `_done()` 统一写；`--history --strict`
     被承认（`asked: true`）而不是拒绝——那页的 cap 要标行，拒绝会砸掉它（边界 410/411）。
158. `--unrecorded` 的空页也要交通道：账本空、全部有文件、有没文件的但都不再决定什么，这三种是一句话三件事，脚本分不出来；`why_empty` 记下来，页面和清单一起说。
     （已完成：空页也写 `--json-out`，`why_empty` 取 `no rounds` / `all placed` / `none deciding`，有活时为 `null`；新增 `_write_unrecorded_payload` 让一页只占 `PAYLOAD_WRITERS` 的一行；扫描器不再把自己的字面量算成一次调用——"24 writers" 曾经是 23 次调用加计数器自己，计数规则抽成 `payload_writer_calls(text)` 供命令与测试共用，见 §3.178 与边界 412/413。）

159. 让 `--unrecorded` 的空页也进清单说明：`why_empty` 的三种取值现在是三句独立的句子，而 `PAYLOAD_WRITERS` 那一行只说 `why_empty`；把三种取值也写进页面（哪一行会说哪一句），并让 `--payload-writers --check` 验证每种取值都在源码里出现一次。
