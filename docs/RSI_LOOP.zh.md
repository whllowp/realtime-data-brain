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
| 提出 | `catalogue()`：7 个候选，每个必须带一个**可被证伪的假设** |
| 评测 | `measure()`：标注数据（recall/FPR）+ 证据覆盖（确定性）+ token 账本，三级信号 |
| 选择 | `decide()`：先硬否决，再要求 Pareto 增益 |
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

### 3.6 接线修好之后，SMD 依然没有信号

`var/smd_signal.py`，828 个计分窗口（134 个故障），走的是生产路径（`Pipeline` → 真实 digest → Laya 的 `noul` 答案）：

```
scored windows 828 (anomalous 134)
noul anomalous  median 0.3709  max 0.4024
noul normal     median 0.3761  max 0.4166
AUC of noul     0.4176
best recall at <=10% FPR: 0.030 (at FPR 0.088)
cut 0.40 ->   20 alerts ( 2.4% of windows),   1 of 134 anomalies reached
cut 0.45 ->    0 alerts ( 0.0% of windows),   0 of 134 anomalies reached
```

异常类不但没有更高，反而略低（AUC 0.4176 < 0.5）。任何能产生告警的切点都在两个类上同时产生告警：0.40 以上只剩 20 个窗口，只捞到 134 个故障里的 1 个。

这也澄清了一个之前被含混过去的数字：第 1–6 轮反复引用的 SMD AUC 0.745 来自 `scripts/real_data/bench_smd.py` **自己的渲染器**（`--digest current/level_range`、3 个特征），不是出厂的 `render_digest(profile="host")`。两者是不同的模型输入。把 bench 的结论当成生产路径的能力，是又一个"同一单位才能比较"的例子——这次比较的单位是提示词。

所以 SMD 上循环同样拒绝：没有信号，就没有可接受的改动。这不是循环的缺陷，正是综述那条"改进强度跟随验证强度"的直接体现——**没有外部信号，就没有可靠的改进**。


---

## 4. 分级：它是 L2，不是 L3

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
4. **在真实数据上它一次接受都没有。** 两条真实域（RCAEval、SMD）上检测器都在机会线上，标签列没有任何可分性，于是循环全部拒绝。合成流上的两次接受（−24.4% / −38.1%）只在该流成立；真实域上的"无结论"是测量事实，不是循环失败，但也不能倒过来说循环在真实数据上有效。
5. **"接受"的含义有限。** 它只说明：在同一条流、同一个评测器、同一份证据池上，一个配置严格优于另一个，且没有丢掉评测器看得见的东西。
6. **真实域的静音可能再次来自接线。** 第 3.5 节已经抓到三个这样的 bug；`digest_specs`/`baseline_keys` 的测试挡住了同一类错误，但挡不住别的（例如未来新增的域需要新的目录或新的基线 key）。

---

## 6. 下一步（按验证强度排序）

1. **让真实数据有信号。** 这是唯一真正的瓶颈：RCAEval 上引擎 AUC 0.44–0.51，SMD 生产路径 AUC 0.4176，两者都低于 0.5。在检测器没有可分性之前，成本策略再优化也只是在优化一个不会发生的告警。具体路径：把 bench 里被验证过的 host 渲染（`--digest current/level_range`、3 个特征）接进 `render_digest(profile="host")`，再用 `var/smd_signal.py` 那种方式复测 AUC——如果接进来仍然 0.41，那就是模型确实不适合这个域，结论不同但同样重要。
2. **真模型校准**：配 API key 后重跑一轮，把 `coverage` 与真实解释质量对比，看这个代理到底代理了什么。
3. **让 `apply` 真生效**（L3 的唯一缺口）：配置热加载 + 自动回滚窗口，才够格说 L3。
4. **让 `Imp` 可变**（L4）：候选目录由循环根据账本自己生成。这需要更强的验证器，否则就是自证循环的教科书案例。
