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

---



```
level            L2
capabilities
  + proposes changes from a fixed catalogue, each with a testable hypothesis
  + evaluates them against labels, prompt evidence and a token ledger
  + records an accepted change as a version file with env and rollback
missing for the next level
  - autonomous implementation: applying an accepted change without a human
```

`grade()` 把"写版本文件"和"改动生效"当成两个事实，只有后者才给 L3。这里导出环境变量、重启服务仍然是人的动作，所以**无论选择环节多自动，它都是 L2**。给一个会自动写 JSON 的系统评 L3，正是综述警告的那种自夸。

`grade()` 刻意不读文件系统、只收事实参数：一个自己报告自己等级的评估器，是自证循环的第一个例子。

---

## 5. 边界（不能宣称的）

1. **不改进检测。** 结构上 System 2 在 `publish decision` 之后才跑，token 花多花少改不了 recall/FPR。循环能优化的只有分析成本与证据完整性。
2. **解释质量没被测。** 无 API key，reasoner 是 `offline`；`coverage` 是"有没有把证据喂进去"的代理，不是"报告写得好不好"。
3. **catalogue 是手写的，目标函数是固定的。** 这两件事都在循环之外，正是它停在 L2/L3 的原因，也是它不变成自证循环的原因。
4. **真实数据上的第一次接受，只有一半被两层证据支撑。** 3.10 节接受了三个候选：`host-features-3` 由标签列（0.060 → 0.187）加成本列同时支撑；`events-8`/`events-4` 只由成本列支撑，因为这条流的证据列结构性失效（无日志可丢）。同时，接受发生在"两台主机的合计"上，而机器 2-2 单独低于机会线；在它被解释清楚之前，这些数字不能外推成"SMD 可检测了"。
5. **"接受"的含义有限。** 它只说明：在同一条流、同一个评测器、同一份证据池上，一个配置严格优于另一个，且没有丢掉评测器看得见的东西。
6. **真实域的静音可能再次来自接线。** 第 3.5 节已经抓到三个这样的 bug；`digest_specs`/`baseline_keys` 的测试挡住了同一类错误，但挡不住别的（例如未来新增的域需要新的目录或新的基线 key）。
7. **quality 这条接受路径只在一条流上验证过一次。** 它由 3 台以上的记录共同决定，而 SMD 只有两台主机：机器 1-6 有可分性（0.674），机器 2-2 低于机会线（0.347），合并后的 at-budget recall 主要由 2-2 决定。所以即使 `host-features-3` 被接受，被接受的也是"两台主机的合计在 10% 误报预算下多捞到一些故障"，不是"SMD 被解决了"。
8. **阈值仍然不是循环能改的。** `anomaly_probability=0.33` 写在流的基线配置里（`scripts/real_data/smd_stream.py` 的 `BASE_OVERRIDES`），因为它是这个域的部署前提，不是候选。循环用来比较的是 at-budget 曲线，它不负责把这条曲线接到线上门限。
9. **at-budget 指标对"只动阈值"的候选是盲的。** 曲线本身是把观测到的概率重扫一遍，所以 `anomaly-p-40`（把门限从 0.33 抬到 0.40）在两条记录上算出完全相同的 recall/FPR——这不是候选无效，而是这个测量方式看不见它。要让阈值类候选可判，需要让 at-budget 之外的某一列（告警量、页面量）也参与比较；这一轮没有做，于是它和另外五个候选一起以 `no-reports` 被拒绝，理由是不充分的，文档必须承认这一点。
10. **状态上限说明"文本里有信号"，不等于"部署能在这个点上告警"。** 3.11 节的读者是一个阈值规则，它的 AUC 只说明排序可分；把它换成线上门限还需要每主机标定、以及误报预算下的实际页面量。3.12 节把这条改动做成了候选并让它被接受，但接受的仍然是一个"在 10% 预算下多捞到故障、并且在 shipped 门限处可用"的配置，不是"SMD 可检测了"。
11. **接受指标与上线指标仍然是两条列，只是多了一道地板。** 3.13 节的 `deploy_fpr_ceiling` 让"shipped 门限处会告警多少"进入了判定，但它只是一条否决线：越线的候选被拒，线内的候选仍然只按 at-budget 曲线比较。所以"接受"依旧不蕴含"上线可部署"，只蕴含"没有明显不可部署"。
12. **读者自己的 0.87 是在同一批窗口上选的。** 所以 3.12 节的那一行验证的是自洽性，不是泛化；换一台主机、换一个月的数据，这个常数不需要重选的证据还不存在。
13. **补丁式候选可以藏一个坏的一半。** 成本列看的是整份补丁，一项降低告警量的伴随改动可以掩护另一项抬高误报率的改动；3.13 节的部署地板挡掉的是"整体不可部署"，挡不住"其中一半是坏的，但另一半把合计拉回线内"。
14. **0.50 这条部署地板是判断，不是测量。** 没有数据支持"误报超过一半即不可部署"这个具体数字；它只挡住了离它很远的配置（3.12 节的 0.935）。把它变成测量需要每页告警的人工成本，本仓库没有这个输入。
15. **重审只重放数字，不重跑。** `--rejudge` 回答的是"今天的规则会怎么判这份记录"，不是"今天的配置今天测出来是什么"；后者要重跑（SMD 全目录约 9 分钟）。

---

## 6. 下一步（按验证强度排序）

1. **解释机器 2-2 为什么低于机会线。** 3.11 节把问题问得更紧了：同一条文本，简单读者在 2-2 上到 0.597，引擎只有 0.347——**信号在文本里，引擎把它排反了**。3.12 节的读者在 2-2 上也只有 0.597，所以下一步应当拿 2-2 的窗口做分层检查（故障时到底是哪个指标在动、引擎在那几行的答案长什么样、长基线是否在吸收故障），而不是继续在合并指标上试配置。
2. **把 `--rejudge` 变成每次改规则的固定动作。** 3.13 节已经能重放历史，但还靠人记得跑；下一步是让"改了 `EvolveRules` 就把全部历史轮次重审一遍"成为一条检查（CI 或 `brian evolve --audit`），并把结果写进账本——否则规则变更本身没有审计轨迹。
3. **按主机分别标定门限。** 现在 `anomaly_probability` 是流里写死的一个常数（3.12 节的 0.87 是两个主机的折中），而 3.7 节已经测出两台主机的分数不在同一把尺上。把门限做成每条记录一份，是让那张表真正进入部署的下一步。
4. **给阈值类候选一个可判的列。** 边界 9/11 指出 at-budget 曲线对"只动阈值"的候选是盲的，`anomaly-p-40` 因此被以不充分的理由拒掉。一个直接读"预算内告警量"的列能让这类候选可判，也能让部署地板从"事后否决"变成"比较的一部分"。
5. **真模型校准**：配 API key 后重跑一轮，把 `coverage` 与真实解释质量对比，看这个代理到底代理了什么。
6. **让 `apply` 真生效**（L3 的唯一缺口）：配置热加载 + 自动回滚窗口，才够格说 L3。
7. **让 `Imp` 可变**（L4）：候选目录由循环根据账本自己生成。3.12 节已经把"提议的形状"放宽过一次（单字段 → 补丁），那是这一方向上安全的一小步；再往前就需要更强的验证器，否则就是自证循环的教科书案例。
