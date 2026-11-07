"""整条请求不能超过模型上下文窗口（ADR-100）。

旧算法只把"历史"算进预算：工具定义、system、记忆、README、摘要、思维链与
输出预留都在窗外。窗口小的时候（例如 32k）这些加起来能有一万，于是
"预算明明没超"的请求会被 API 直接 400。这组测试钉住新口径：

    estimate(工具 + 消息) + max_tokens <= context_window

并且"装不下"必须是**本地**中止 —— 一条注定 400 的请求不许发出去。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.llm_types import (
    Message,
    estimate_message_tokens,
    estimate_messages_tokens,
    estimate_tools_tokens,
)

from .helpers import Harness, make_harness, test_settings, text_reply, tool_reply, usage


def request_size(call) -> int:
    """一次 provider 调用的"输入"估算：消息 + 工具定义。"""
    return estimate_messages_tokens(call["messages"]) + estimate_tools_tokens(
        call["tools"]
    )


class RequestFitsWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _harness(self, script, **overrides) -> Harness:
        harness = make_harness(self.tmp, script, **overrides)
        self.addCleanup(harness.close)
        return harness

    def _assert_all_calls_fit(self, harness: Harness) -> None:
        self.assertTrue(harness.provider.calls, "至少应该发出一次请求")
        for index, call in enumerate(harness.provider.calls):
            self.assertIsNotNone(call["max_tokens"], f"第 {index} 次请求没带 max_tokens")
            self.assertLessEqual(
                request_size(call) + int(call["max_tokens"]),
                harness.settings.context_window,
                f"第 {index} 次请求（输入 + 输出）超过了窗口",
            )

    def test_summary_and_chat_requests_both_fit_a_narrow_window(self) -> None:
        """压缩调用与对话调用**都是** API 请求，两条都得装进同一个窗口。"""
        harness = self._harness(
            [text_reply("摘要"), text_reply("好")],
            context_window=8_000,
            context_token_budget=1_000_000,
            max_output_tokens=1_024,
            compact_max_input_chars=100_000,
        )
        for index in range(20):
            harness.log.append("user_message", text=f"第 {index} 段：" + "很长" * 800)
            harness.log.append(
                "assistant_message",
                step=1,
                finish_reason="stop",
                usage=usage(total=10).to_dict(),
                message=Message(role="assistant", content="记下了。").to_record(),
            )
        harness.loop.run("现在呢")

        self._assert_all_calls_fit(harness)
        self.assertGreaterEqual(len(harness.provider.calls), 2, "应该发生过一次压缩")
        self.assertEqual(
            harness.log.read_events()[-1]["kind"], "turn_end", "收尾必须照常"
        )

    def test_every_step_of_a_tool_turn_fits(self) -> None:
        """工具结果会让历史在**同一轮内**变长：每一步都要重新收进窗口。"""
        harness = self._harness(
            [
                tool_reply([("c1", "mem_create", '{"content":"喜欢美式咖啡"}')]),
                text_reply("好"),
            ],
            context_window=6_000,
            context_token_budget=1_000_000,
            max_output_tokens=1_024,
        )
        harness.loop.run("记住我喜欢美式咖啡")

        self.assertEqual(len(harness.provider.calls), 2)
        self._assert_all_calls_fit(harness)

    def test_impossible_request_is_never_sent(self) -> None:
        """窗口比"工具 + system + 一条超大消息"还小：本地中止，不发请求。"""
        harness = self._harness(
            [text_reply("不该被调用")],
            context_window=2_000,
            context_token_budget=1_000_000,
            max_output_tokens=512,
        )
        outcome = harness.loop.run("长" * 50_000)

        self.assertEqual(harness.provider.calls, [], "装不下时不许发请求")
        self.assertEqual(outcome.stop_reason, "context_limit")
        notices = [
            record["text"]
            for record in harness.log.read_events()
            if record["kind"] == "notice"
        ]
        self.assertTrue(any("上下文窗口" in text for text in notices))
        self.assertEqual(
            harness.log.read_events()[-1]["kind"],
            "turn_end",
            "本地中止也要留下 turn_end，不能像从未结束过",
        )

    def test_output_cap_is_lowered_instead_of_overflowing(self) -> None:
        """输入装得下、但"输入 + 原输出上限"装不下时，压低输出上限继续发。"""
        harness = self._harness(
            [text_reply("好")],
            context_window=4_000,
            context_token_budget=1_000_000,
            max_output_tokens=2_048,
        )
        harness.loop.run("短问题")

        call = harness.provider.calls[-1]
        self.assertLess(call["max_tokens"], 2_048, "窗口不够时要压低输出，而不是硬发")
        self.assertLessEqual(request_size(call) + int(call["max_tokens"]), 4_000)
        notices = [
            record["text"]
            for record in harness.log.read_events()
            if record["kind"] == "notice"
        ]
        self.assertTrue(any("输出上限" in text for text in notices), "降上限要说出来")


class MeasuredOverheadTests(unittest.TestCase):
    """用本机实测的固定开销做一次回归（ADR-100 的数字来自这里）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_a_32k_window_cannot_be_overflowed_by_the_old_accounting(self) -> None:
        """32k 窗口 + 9 个工具 + 满记忆 + 满 README：旧算法会算出 24k 历史预算。

        实测固定开销约 10.8k（工具 1967 + system 8822 + 尾巴 20），
        24k + 10.8k + 输出 2048 = 36.9k > 32k。新算法必须把它压回窗口以内。
        """
        settings = test_settings(
            self.tmp,
            context_window=32_000,
            context_token_budget=1_000_000,
            max_output_tokens=2_048,
        )
        overhead = 10_809
        budget = settings.history_budget(overhead_tokens=overhead)
        self.assertLess(budget, 24_000, "固定开销占了窗口，历史预算必须变小")
        self.assertLessEqual(
            budget + overhead + settings.max_output_tokens + settings.context_margin(),
            32_000,
        )


class EstimateTests(unittest.TestCase):
    def test_tool_definitions_are_counted(self) -> None:
        """工具定义每轮都发，必须算进预算（旧算法把它们当免费的）。"""
        self.assertEqual(estimate_tools_tokens([]), 0)
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "x" * 50,
                    "description": "y" * 1_000,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        self.assertGreater(estimate_tools_tokens(tools), 200)

    def test_reasoning_content_is_counted(self) -> None:
        """思维链会随 reasoning_content 真的发出去，不能不算（ADR-100）。"""
        bare = Message(role="assistant", content="答")
        with_reasoning = Message(role="assistant", content="答", reasoning="想" * 1_000)
        self.assertGreater(
            estimate_message_tokens(with_reasoning),
            estimate_message_tokens(bare) + 900,
        )


if __name__ == "__main__":
    unittest.main()
