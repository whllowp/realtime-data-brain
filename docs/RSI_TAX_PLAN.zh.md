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

## 第 2–10 轮计划

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
