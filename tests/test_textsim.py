"""文本相似度原语的测试（原 `test_recall.py`）。

这个文件原来叫 `test_recall.py`，服务于 M2 的"记忆召回"机制 —— 那套机制已经
整删掉了（ADR-058：Kimi 的 memory instructions 是"全量注入 + 硬上限"，
我们多做了一层按相关性挑选，多出来的复杂度不值那个钱）。

`harness/textsim.py` 里的三个原语留了下来，因为它们的用途**本来就不是召回专属**。
M7 之后它只剩一个消费者：

* `vault.py` —— `dream_search` 在背景档案里做字面检索。

（以前还有 `dream.py` 用它们挑"看起来几乎重复"的记忆候选对，那套手术机制
随 ADR-059 退役了。）

所以这里只保留"原语本身"的用例（把它们钉死，因为 `dream_search` 建立在它们的行为上），
以及一条**已知局限**：这些原语跨不过同义/上下位词，而那正是设计上接受的取舍 ——
语义鸿沟改由模型自己换关键词来跨（`dream_search` 的说明里就写着这一点）。
召回策略（`plan_recall` / 排序 / 门槛 / 预算 / 检索块位置）的用例随机制一起删除。
"""

from __future__ import annotations

import unittest

from harness.textsim import bigrams, containment, dice, normalize


class NormalizeTests(unittest.TestCase):
    def test_strips_all_whitespace_and_folds_case(self) -> None:
        self.assertEqual(normalize("  Hello  World "), "helloworld")
        self.assertEqual(normalize("小林\t喜欢\n美式"), "小林喜欢美式")
        self.assertEqual(normalize("ABC"), "abc")

    def test_empty_inputs_are_empty(self) -> None:
        # 空串与 None 都要能安全穿过（调用方常常是"可能没填"的字段）
        self.assertEqual(normalize(""), "")
        self.assertEqual(normalize(None), "")  # type: ignore[arg-type]
        self.assertEqual(normalize("   "), "")


class BigramTests(unittest.TestCase):
    def test_bigrams_of_cjk(self) -> None:
        self.assertEqual(bigrams("咖啡"), {"咖啡"})
        self.assertEqual(bigrams("美式咖啡"), {"美式", "式咖", "咖啡"})

    def test_short_inputs(self) -> None:
        """长度不足 2 时退化成那一个字本身，而不是空集合。

        这个退化是必要的：`dream_search("猫")` 这种单字查询如果得到空集合，
        检索会直接返回"什么都没找到"，而用户的档案里明明有"猫"。
        """
        self.assertEqual(bigrams(""), set())
        self.assertEqual(bigrams("a"), {"a"})
        self.assertEqual(bigrams("猫"), {"猫"})

    def test_whitespace_does_not_break_matching(self) -> None:
        """空白与大小写不该影响匹配 —— 中文没有词边界，空格只是排版噪声。

        （这条原来是通过 `score_item` 间接验证的；打分函数删掉之后，
        改为直接钉在生成二元组这一层，断言的是同一个性质。）
        """
        tight = bigrams("小林喜欢美式咖啡")
        loose = bigrams("小林 喜欢 美式 咖啡")
        self.assertEqual(tight, loose)
        self.assertEqual(bigrams("Coffee"), bigrams("c o f f e e"))


class DiceTests(unittest.TestCase):
    def test_bounds(self) -> None:
        self.assertEqual(dice({"a", "b"}, {"a", "b"}), 1.0)
        self.assertEqual(dice({"a"}, {"b"}), 0.0)
        self.assertEqual(dice(set(), {"a"}), 0.0)
        self.assertEqual(dice({"a"}, set()), 0.0)
        self.assertEqual(dice(set(), set()), 0.0)

    def test_is_symmetric(self) -> None:
        left = bigrams("小林喜欢美式咖啡")
        right = bigrams("小林喜欢手冲咖啡")
        self.assertEqual(dice(left, right), dice(right, left))

    def test_near_duplicates_score_high(self) -> None:
        """dream 的预筛靠这个性质：只差一个词的句子应当被挑成候选。"""
        score = dice(bigrams("小林喜欢美式咖啡"), bigrams("小林喜欢手冲咖啡"))
        self.assertGreater(score, 0.5)


class ContainmentTests(unittest.TestCase):
    def test_is_asymmetric(self) -> None:
        query = {"咖啡", "喜欢"}
        content = {"小林", "喜欢", "喝美式", "咖啡"}
        self.assertEqual(containment(query, content), 1.0)
        self.assertLess(containment(content, query), 1.0)

    def test_empty_query_is_zero(self) -> None:
        self.assertEqual(containment(set(), {"a"}), 0.0)

    def test_no_overlap_is_zero(self) -> None:
        self.assertEqual(containment({"咖啡"}, {"量子"}), 0.0)


class KnownLimitationTests(unittest.TestCase):
    """**已知局限**：字面相似度跨不过同义 / 上下位词。

    这不是 bug，是当初就接受的取舍（ADR-016 → ADR-058 之后更明确）：
    零依赖的字面匹配 + **让模型自己换关键词**，比引入分词/向量检索划算得多。
    把行为固定下来，是为了让这个局限**可见**：哪天真的要做语义检索，
    这条测试就该被改写，而不是继续假装不存在。
    """

    def test_synonyms_share_nothing(self) -> None:
        # 「我会什么乐器？」与「小林会弹吉他」一个字都不重叠
        self.assertEqual(dice(bigrams("我会什么乐器？"), bigrams("小林会弹吉他")), 0.0)
        self.assertEqual(
            containment(bigrams("我会什么乐器？"), bigrams("小林会弹吉他")), 0.0
        )

    def test_literal_overlap_still_works(self) -> None:
        """换成字面有重叠的问法就能匹配 —— 所以"换关键词"是有效的补救。"""
        self.assertGreater(
            containment(bigrams("我会弹什么？"), bigrams("小林会弹吉他")), 0.0
        )


if __name__ == "__main__":
    unittest.main()
