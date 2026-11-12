# 参考《The RSI Tax》的 10 轮优化

> 论文：[The RSI Tax: When Recursive Self-Improvement Is Not Worth the Cost](https://arxiv.org/html/2611.13524v1)
> （arXiv:2611.13524v1，Zhang / Li / Wang，CC BY 4.0）

## 论文说了什么（只摘与本项目相关的）

1. **RSI Tax**：自我改进过程的 token 成本必须计入端到端评估。端到端 = `N_task + N_RSI`，只报准确率而不计改进成本，会让昂贵的流程看起来免费。
2. **SRN 基线**：一个有界笔记（2,048 token，每 4 个任务用失败任务修订一次），在四个设置中三个准确率最高、一个并列，同时比 Meta-Harness / EvoSkill / RRSI 少用 **65.6–71.0%** 的总 token。
3. **成本分解**：`Verify`（在已完成任务上验证候选）、`Explore`（采样/提案）、`Write`（落盘状态）。三种 RSI 方法的验证开销占 RSI Tax 的 **74.8–93.6%**。
4. **未被使用的产物**：23.7–63.6% 的 RSI Tax 花在后续任务从未使用的状态/产物上。

## 映射到本项目（必须先说清楚，因为场景不同）

| 论文 | 本项目 |
|---|---|
| `N_task`（任务执行） | Laya 决策。本地固定模型，无 token，成本是 GPU 时间 |
| `N_RSI`（改进过程） | System 2：每个升级告警一次 LLM 调用。**这才是账单** |
| RSI Tax | **每决策的 token 数** |
| 未被使用的产物 | **挂在不会触达人的告警上的报告** |
| Verify / Explore / Write | 本项目每次报告只调用 1 次 LLM，因此是 **100% Write**，0 Verify，0 Explore |

单位从不混用：token 与毫秒分开报，比值只在同一单位内计算。

---

## 第 1 轮：把成本账建起来（此前完全没有）

### 1.1 发现：token 早就被计数，但从未进入评估

`LlmClient` 一直在累计 `prompt_tokens` / `completion_tokens`，`RcaEngine` 也有 `stats()`；
但 `eval.py`、`pipeline.py`、所有 10 轮 SMD 迭代**一次都没有读过**。这正是论文指出的记账失败：
昂贵的半边在账本之外。

### 1.2 落地

| 文件 | 改动 |
|---|---|
| `src/brian/contracts.py` | `RcaReport` 新增 `prompt_tokens` / `completion_tokens` / `estimated_prompt_tokens` |
| `src/brian/llm/rca.py` | LLM 路径填入实际 token；确定性路径填入**提示词估算值**（该估算器原本只用于超长告警） |
| `src/brian/pipeline.py` | `Counters` 累计 `rca_prompt_tokens` / `rca_completion_tokens` |
| `src/brian/cost.py` | `CostLedger`：按动作归因、论文口径的 Verify/Explore/Write 分解、unused share |
| `src/brian/cli.py` | 新增 `brian cost` |
| `tests/test_cost.py` | 7 个测试守护口径（不混单位、估算不覆盖真实账单、计数器路径不假装能归因） |

### 1.3 基线（合成流，35 窗口）

```
windows            35
laya decisions     35  (0.00 ms/window)
rca reports        8  (8 degraded)
tokens             31725  (prompt estimate; nothing billed - deterministic mode)
tokens / report    3965.6
tokens / window    906.4
split              verify 0  explore 0  write 31725  other 0
unused share       37.4%
by action:
  deep_analysis  reports 3    tokens 11854
  page_human     reports 5    tokens 19871
```

三个立刻可用的结论：

1. **单次分析约 3,966 token，每窗口 906 token** —— 这是 RSI Tax 的本项目版本。
2. **37.4% 的分析预算花在 `deep_analysis` 告警上**，那不是会把报告放到人面前的动作用。这是论文 unused-artifact 的对应量，且是**下界**（严格口径：只有 `page_human` 算"被使用"）。
3. **分解是 100% Write** —— 本项目没有验证与探索预算。论文里验证占了 74.8–93.6%，而这里是一次调用直接写报告。**结构上已经站在便宜的一侧**，这一点在"优化"之前必须先承认。

### 1.4 这一轮没有做的事

- 没有接真实 LLM 跑（需要 API key），所以 `tokens` 是**提示词估算**，明确标注。完成侧 token 无法离线估算。
- 没有动 `always_analyze_alerts`（当前为 True），那正是下一轮要测的旋钮。

---

## 第 2 轮：修正一个错误结论 + 按动作路由分析

### 2.1 先纠错：第 1 轮的"unused share"是错的

第 1 轮报"37.4% 的分析预算花在未被使用的产物上"。读动作阶梯的定义才发现**这是误判**：

| 动作 | 定义原文 | 报告是什么 |
|---|---|---|
| `auto_triage` | "confident System 1 judgement: **LLM writes the narrative**" | 交付物 |
| `deep_analysis` | "not confident enough to act on the class/severity: **the LLM must reason harder**" | 交付物 |
| `page_human` | "page a human in parallel with the LLM analysis" | 交付物 |

三者都需要报告，**没有一个是"产生后就被丢弃的产物"**。论文的 `r_no-use` 在本系统不成立。

指标已改名 `share_below_page`（不触达人的告警占比），并写明它**不是** `r_no-use`：
它衡量的是"从不打扰任何人的花销"，这正是"更便宜的叙述策略"该瞄准的部分，但不是"浪费"。

### 2.2 真正的机会：阶梯已经分好了需求程度

`auto_triage` 只要叙述，`deep_analysis` 要推理，`page_human` 两者都要。
为 `auto_triage` 付一次完整推理，正是论文测到的"不需要的验证"。于是新增
`GateThresholds.analyze_actions`（默认全部，保守），`brian cost` 可对比策略：

| 策略 | 计费报告 | tokens | 每窗口 | 相对现状 |
|---|---|---|---|---|
| **A 全部**（现状） | 8 | **31,725** | 906.4 | — |
| B `deep_analysis`+`page_human` | 8 | 31,725 | 906.4 | **0%** |
| **C 仅 `page_human`** | 5（+3 走规则） | **19,871** | 567.7 | **−37.4%** |

两个结果都要说清楚：

1. **B 省不到钱**，因为这条合成流里根本没有 `auto_triage` 告警——这是**流的属性，不是策略的失败**。
2. **C 省 37.4%，代价正是阶梯要求的推理**。`deep_analysis` 的字面意思就是"LLM 必须更深入推理"，
   把它路由到规则，等于用成本换掉那 3 个告警的推理质量。

所以 C 不是"优化"，是**一个需要运维判断的取舍**。论文的价值也在这里：把账单摆出来，让取舍是自觉的。

### 2.3 保证：跳过分析不等于静默

新增的 `RcaEngine.analyse_rules()` 让被路由掉的告警**仍然拿到报告**（确定性推理器写），
只是不计 token。没有这条，"省钱"会伪装成"少解释"——那才是真正的退化。

测试：`tests/test_analysis_routing.py`（解析容错、空集合合法、默认必须保守）。


| 轮次 | 目标 | 手段 | 验收 |
|---|---|---|---|
| 2 | 量化"按动作门控"能省多少 | 对比 `always_analyze_alerts=True/False`、以及只对 `page_human` 分析 | 省钱比例 + 准确率/可解释性损失 |
| 3 | 有界笔记（SRN 类比） | 用历史告警生成 ≤2,048 token 的处置笔记，替代每次重新分析 | token 降幅 + 决策质量 |
| 4 | 验证成本为零是否可靠 | 抽样人工复核 vs 规则校验，测"省掉验证"带来的错误率 | 错误率变化 |
| 5 | 归因到告警类型 | 按 7 个场景分解 token，找最贵的类别 | 每类 token/报告 |
| 6 | 与阈值法对比端到端成本 | Laya+LLM vs 纯阈值告警的总成本 | 同准确率下的成本比 |
| 7 | 缓存与去重 | 同 `dedup_key` 的连续窗口是否重复分析 | 重复率与可省 token |
| 8 | 真实 LLM 校准 | 配置 API key，用实际 usage 替换估算 | 估算误差 |
| 9 | 回归保护 | 合成集 0.854/0.000 不受影响 | 无回归 |
| 10 | 终局 | 首轮 vs 末轮成本-准确率对照表 | 一张表 + 明文边界 |
