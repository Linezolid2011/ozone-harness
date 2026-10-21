# M2 记忆召回设计说明 〔已废弃：整套机制已删除，见 ADR-058〕

> **这份文档现在只剩史料价值。** 2026-10-11 起，"超过 30 条就检索"这一整套机制
> 连同记忆别名一起删掉了：Kimi.com 的 memory instructions 是
> **≤50 条 + 全量注入 + 硬上限**，没有检索、也没有别名。
> 我们当初假设"记忆会涨到几百条"，多做了这一层，而它**自己制造**了一个
> 本来不存在的问题（语义鸿沟），再需要别名去补 —— 详见 `docs/01-decisions.md` ADR-058。
>
> 现在的行为：**永远全量注入**，条数上限 0–100 可调（ADR-057），
> 满了以后 `mem_create` 直接失败，由模型改用 `mem_edit` 合并。
> 打分的三个函数（`bigrams` / `dice` / `containment`）搬到了
> `harness/textsim.py`，仍被背景档案检索与 dream 去重使用。
>
> 下面保留原文 —— 它记录的"为什么当初那么做"仍然是真实的，
> 而且**第 3 节"打分：可解释优先"和第 4 节的数据流里对缓存的分析**，
> 在别处（vault 检索、ADR-056 的注入位次）仍然是有效经验。

## 要解决的问题

M1 是**全量注入**：所有有效记忆都塞进 system 消息。
记忆少时这没问题，甚至是最优解（完整、稳定、缓存友好）。
但记忆条数与 token 成本线性增长——200 条记忆每轮要多付几千 token，
而且无关记忆会稀释模型的注意力。

> （事后看，前一句就是这一层机制的全部理由，而它建立在
> "记忆会涨到 200 条"这个**我们自己的假设**上。Kimi 用硬上限把条数卡在 50，
> 于是这个前提根本不成立。）

## 三条设计原则

### 1. 自适应：小库不检索

```
记忆条数 ≤ HARNESS_FULL_INJECTION_LIMIT(30)  →  全量注入（与 M1 完全一致）
记忆条数 >  阈值                              →  核心档案 + 相关性检索
```

小库走检索只有坏处：请求结构变得每轮不同、缓存退化、还可能漏掉本可直接看到的记忆。
所以阈值存在的意义是**保证不退化**，而不是省那点 token。

### 2. 位置：检索结果紧跟当前提问，绝不能放最前面

prompt cache 按 token 前缀命中（见 ADR-017 的推演与实测）。
把易变的检索块放在最前面，等于每轮作废整段历史缓存。

实现上由 `context.insert_before_last_user` 保证，并且有两条测试守住：
`test_recall_block_does_not_move_between_steps`（轮内不移位）与
`test_consecutive_requests_share_everything_but_the_last_group`（轮间前缀不退化）。

### 3. 打分：可解释优先

```
score = 0.7 × Dice(查询二元组, 记忆二元组)
      + 0.3 × 覆盖率(查询二元组 ∩ 记忆二元组) / |查询二元组|
```

- 用**字符二元组**而不是分词：中文没有词边界，bigram 天然可用且零依赖。
- 覆盖率项是不对称的，专门奖励"记忆覆盖了用户问到的点"。
- **时间新鲜度只加进排序分，不参与入选判定**。
  否则每条新记忆都会在每个无关问题里被注入——这有一条专门的测试守着。
- 排序平局按 id 升序，保证确定性：同样的输入必须给出同样的注入顺序，
  否则测试不稳定，缓存也会白白失效。

入选门槛 `HARNESS_RECALL_MIN_SCORE`（默认 0.08）作用于**字面分**，不是最终分。

## 数据流

```
AgentLoop.run(user_text)
  └─ plan_recall(active_items, user_text, ...)         一轮只做一次
       ├─ 模式判定：empty / disabled / full / split
       ├─ split: 核心池 = category ∈ 核心分类，按 id 升序装到 core_budget
       ├─ split: 候选 = 其余条目 → 打分 → 门槛过滤 → top_k → 装到 recall_budget
       └─ 返回 RecallPlan(core, hits, notes)
  ├─ core_text   → 进 system 消息（整轮固定）
  ├─ recall_text → context.insert_before_last_user（整轮固定）
  └─ 决策写入 prompt_built 事件（recall_mode / recall_ids / recall_summary）
```

## 调参工具

```bat
run.cmd --recall "我平时喝咖啡有什么讲究？"          :: 前 5 名 + 入选标记
run.cmd --recall "我平时喝咖啡有什么讲究？" --recall-all  :: 全部候选
python -X utf8 scripts/cache_report.py <session-id>  :: 按步缓存命中
python -X utf8 scripts/seed_demo_memories.py --data-dir data-m2 --filler 155
```

`--recall` **完全不调用模型**，可以反复试。
质量回归由 `tests/test_recall.py` 里的离线评测回答：
30 条记忆 + 12 个带标注的问题，断言 Top-3 必含正确答案、Top-1 命中率 ≥ 80%。

## 已知局限

- **同义/上下位词跨不过去**：查询「我会什么乐器？」召不回「小林会弹吉他」；
  查询写「喜欢喝」也召不回记忆里写的「偏好喝」（两词无任何字符重叠）。
  这不是调参能解决的——加单字重叠也只共享一个「喝」字，加权 0.025 < 门槛 0.08。
  局限已写成测试固定住（`test_known_limitation_semantic_gap`），
  等引入向量检索时那条测试就该被改写。
- **记忆质量决定检索质量**：字面相关但无意义的条目照样会被检索出来。
  实测中一批"负责过 XX 模块"的填充记忆因为与"工作上负责什么"共享「负责」而被拉进上下文。
  这类噪声靠提高 `HARNESS_RECALL_MIN_SCORE` 或降低 `HARNESS_RECALL_TOP_K` 缓解，
  但根源在记忆本身的质量。
- 没有做查询改写、没有多轮查询融合（只取当前这一轮的 user 文本）。
