# 记忆系统：对照 Anthropic《The components of agent memory》

参考：<https://www.anthropic.com/research/components-of-agent-memory>

## 一、文章的框架

四层记忆层级，外加一个离线整理过程（*dreaming*）：

| 层 | 文章中的角色 | 关键约束 |
|---|---|---|
| **transcripts** | 可检索的事件日志 | 是记忆的**事实来源**，笔记要对着它校验 |
| **memory notes** | 模型写下的要点 | 会漂移；必须能被日志证伪 |
| **representations** | 索引 / wiki，聚合关联知识 | 小而精胜过大而全 |
| **injected context** | 每次会话都加载的块 | **必须很小，且只放索引不放正文** |
| **dreaming** | 空闲时离线整理 | 校验笔记、建表示、塑造注入层 |

### 文章里三条可被本项目的实证检验的结论

1. **"只记失败"这条固定规则会伤害强模型**：文章测得 −0.234。让模型自己判断什么值得记，
   比硬编码规则好。
2. **小而精 > 大而全**：Sonnet 3.7 写了 413 个文件 / 978k 字符 → 1 个 badge；
   Fable 5 只写 10 个文件 / 43k 字符 → 4 个 badge。
3. **注入层只能是索引**：Claude Code 的 auto memory 只注入 MEMORY.md 的前 200 行 / 25 KB。

## 二、本项目的现状对照（改动前）

| 层 | 本项目对应物 | 状态 |
|---|---|---|
| transcripts | `brian/bus.py` 的 Journal/MemoryBus，offset 可重放 | ✅ 已有 |
| memory notes | `brian/notes.py` 的 `NoteBook`（2,048 token 上限） | ⚠️ 有，但策略是"只在失败时修订" |
| representations | —— | ❌ **完全没有** |
| injected context | `prompts.build_rca_messages(note=...)` | ⚠️ 有通道，但没有内容、没有索引形态 |
| dreaming | —— | ❌ **完全没有** |

两处直接踩坑：

- `NoteBook.should_revise(failed)` **正是**文章说会伤害强模型的那条固定规则。
- 注入层只有一个自由文本的 note，没有大小纪律、没有索引形态。

## 三、本轮做了什么：`src/brian/dreaming.py`

### 3.1 representation 层：`IncidentIndex`

从**转写**（decisions + rca）聚合出 `(service, category)` 一行，含出现次数、最近时间、授权过的动作、
一条代表性根因。它回答的是"这个系统里**反复发生的是什么**"，而不是"刚才发生了什么"。

### 3.2 injected 层：`IncidentIndex.render_injected()`

输出**索引**，不是散文：每行是"服务|类别 x次数 最近时间 [动作] 一句话"，总量硬上限
`INJECTED_MAX_TOKENS = 400`。超出时不静默截断，而是写明 `... N more patterns omitted`。

按出现次数排序，所以它描述的是**复发模式**，不是变更日志。

### 3.3 dreaming：`dream(records, note_text)`

离线跑一遍：

1. 从转写重建索引；
2. **逐行校验笔记**——提到服务/类别名但转写里不存在的主张，标为无据并删除，记录原因；
3. 生成注入块。

只做**删除**，不改写。理由：凭转写只能判断"这条主张是否还有据"，改写需要模型，硬做等于伪造。

### 3.4 一次真实的误杀

第一次跑，校验器把 `checkout-api resource_exhaustion: pool exhaustion shows as latency before errors.`
**误删了**——因为 `checkout-api` 被分词成 `checkout` + `api`，而校验是整词匹配，索引里那 7 次
`checkout-api|resource_exhaustion` 匹配不上。

**一个删掉真实记忆的校验器，比没有校验器更糟**，因为它看起来像在尽职。已改为子串匹配，并加了
钉死测试。修正后：4 条主张、删 1 条（`payment-gateway` 确实不在转写里）。

### 3.5 实测（合成流，35 窗口）

| 项 | 数值 |
|---|---|
| 转写记录 | 51（decisions + rca + alerts） |
| 索引条目 | 7（渲染 6，1 条仅报告无判定被隐藏） |
| 主张校验 | 4 条，删 1 条 |
| 注入块 | **153 token**（上限 400） |

注入块成本：153 / 4,828 ≈ **+3.2% 每个提示词**。与第 3 轮"有界笔记"同量级——
所以它同样**不接入默认路径**，需要先证明解释质量因此改善，而那不是没有模型能回答的。

## 四、和"记失败"那条规则的冲突

第 3 轮的 `NoteBook.should_revise(failed)` 要求"有失败才修订"，而文章实测这条规则让强模型
**下降 0.234**。本轮**没有**去改这个策略，因为：

- 文章测的是"让模型自己判断显著性"，本项目没有模型可用，改了也只是换一条硬编码规则；
- 真正的修正是让**内容正确**（本轮做的），策略留待有模型时再谈。

这一点写在这里是为了不让它变成"已解决"。

## 五、仍然缺的

1. **没有真正的 search 层**。文章的关键发现是"记忆 + 检索 > 只用记忆"（54% → 61%），
   而本项目没有"检索相似历史事件"的能力——只有聚合索引。这是下一个该做的组件。
2. **没有多页表示**。文章的例子是 wiki（互链的页面）；本项目的索引是单文件扁表。
3. **没验证解释质量**。所有测量都是结构与体积，没测"注入了索引之后报告是否更好"——需要真实 LLM。

## 六、复现

```bash
python scripts/dream_demo.py
# 产出 var/memory/{incident_index.json, injected.md, note.md, dream_report.json}
```

测试：`tests/test_dreaming.py`（8 个，含误杀回归、上限与截断可见性、无转写不误删）。
