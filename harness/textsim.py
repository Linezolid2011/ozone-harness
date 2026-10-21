"""文本相似度：字符二元组（bigram）与两个简单的重叠度量。

**这个模块不再是"记忆召回"**。它原来叫 `recall.py`，服务于 M2 的
"记忆超过 30 条就只注入最相关的几条"那套机制 —— 那套机制已经删掉了
（见 ADR-058：Kimi 的 memory instructions 是全量注入 + 硬上限，我们多做了）。

留下这三个函数是因为它们**本来就不是召回的专属物**，而是"零依赖的中文字面相似度"
这个通用工具。M7 之后它只剩**一个**消费者：

* `vault.py`：在背景档案里做字面检索（`dream_search`，给整理者和对话模型用）。

（以前还有第二个：`dream.py` 用它挑出"看起来几乎重复"的记忆候选对。那套手术机制
随 ADR-059 整体退役，所以那条用途也没了 —— 这里留着说明是为了避免下次有人
再照着旧文档去找一个不存在的调用点。）

## 为什么是字符二元组，而不是分词/向量

* **FTS5 对中文是坏的**：默认 unicode61 分词器把连续汉字当成**一个** token，
  一句中文记忆会变成一个巨大的词元，中文检索基本失效；
* 字符二元组（bigram）对中文天然有效，而且**零依赖、可解释、可离线评估**；
* 语义鸿沟（"乐器" ↔ "吉他"）它跨不过去 —— 所以我们**不去跨**：
  该由模型在需要时自己换关键词（`dream_search` 的说明里就写着这一点）。
"""

from __future__ import annotations

import re

_WHITESPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """去掉所有空白并统一大小写 —— 中文没有词边界，空白只会干扰 bigram。"""
    return _WHITESPACE.sub("", text or "").casefold()


def bigrams(text: str) -> set[str]:
    normalized = normalize(text)
    if len(normalized) < 2:
        return {normalized} if normalized else set()
    return {normalized[index : index + 2] for index in range(len(normalized) - 1)}


def dice(left: set[str], right: set[str]) -> float:
    """Dice 系数：整体字面相似度（对称）。"""
    if not left or not right:
        return 0.0
    return 2 * len(left & right) / (len(left) + len(right))


def containment(query_grams: set[str], content_grams: set[str]) -> float:
    """查询的二元组有多大比例**原样出现**在内容里（不对称）。

    那个不对称是刻意的：它奖励"内容覆盖了查询问到的点"，
    而不像 `dice` 那样被内容本身的长度稀释。
    """
    if not query_grams:
        return 0.0
    return len(query_grams & content_grams) / len(query_grams)
