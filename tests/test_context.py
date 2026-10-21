"""上下文组装测试：system 结构、记忆注入、按组裁剪的完整性。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from harness.context import (
    build_messages,
    build_system_message,
    build_tail_message,
    split_into_groups,
    trim_history,
)
from harness.llm_types import Message, ToolCall, estimate_tokens

from .helpers import test_settings


def _tool_round(index: int) -> list[Message]:
    call_id = f"call_{index}"
    return [
        Message(role="user", content=f"第 {index} 个问题，内容稍微长一点点用来占预算"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id=call_id, name="mem_create", arguments='{"content":"x"}')],
            reasoning="这是要回传的思维链",
        ),
        Message(role="tool", tool_call_id=call_id, name="mem_create", content='{"ok": true}'),
    ]


class SystemMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = test_settings(Path(self._tmp.name))

    def test_contains_memory_and_rules_but_no_time(self) -> None:
        message = build_system_message(self.settings, "[mem:1] (偏好) 用户喜欢美式咖啡")
        self.assertEqual(message.role, "system")
        self.assertIn("[mem:1]", message.content)
        self.assertIn("mem_create", message.content)
        # 时间**必须不在** system 里：它每轮都变，而 system 之后还有摘要与全部历史，
        # 放进去等于每跨一分钟就把那些内容的缓存全部作废（命中价是未命中的 1/50）。
        self.assertNotIn("当前时间", message.content)

    def test_time_is_the_last_message(self) -> None:
        now = datetime(2026, 10, 4, 21, 0, tzinfo=timezone.utc)
        messages, _ = build_messages(
            self.settings, _tool_round(1), "(空)", now=now
        )
        self.assertIn("当前时间", messages[-1].content)
        self.assertIn("2026-10-04", messages[-1].content)
        self.assertNotIn("当前时间", messages[0].content)

    def test_omitted_note_only_when_trimmed(self) -> None:
        clean = build_tail_message(now=datetime.now().astimezone())
        self.assertNotIn("已省略", clean.content)
        trimmed = build_tail_message(
            now=datetime.now().astimezone(), omitted_groups=3
        )
        self.assertIn("更早的 3 段对话", trimmed.content)

    def test_prefix_survives_a_minute_change(self) -> None:
        """跨分钟不能让历史失去缓存。

        时间放在 system 里时，只要跨一分钟，它后面的摘要与整段历史就全部作废
        （缓存命中价是未命中的 1/50，而历史是大头）。放在末尾则只有那一小块是新的。
        """
        history = _tool_round(1)
        at_2200, _ = build_messages(
            self.settings, history, "(核心)", now=datetime(2026, 10, 4, 22, 0, tzinfo=timezone.utc)
        )
        at_2201, _ = build_messages(
            self.settings, history, "(核心)", now=datetime(2026, 10, 4, 22, 1, tzinfo=timezone.utc)
        )

        self.assertNotEqual(at_2200[-1].content, at_2201[-1].content)
        self.assertEqual(
            [message.to_api() for message in at_2200[:-1]],
            [message.to_api() for message in at_2201[:-1]],
            "除了末尾的时间块，其余部分必须逐字节相同",
        )

    def test_volatile_content_never_precedes_the_history(self) -> None:
        """顺序即省钱：变化慢的在前、变化快的在后，才能最大化命中 prompt cache。

        时间与"已省略 N 段"都只出现在**最后一条**消息里；主 system 只放稳定内容。
        """
        history = _tool_round(1) + _tool_round(2)
        messages, _ = build_messages(self.settings, history, "[mem:7] (偏好) 用户喜欢美式")
        content = [message.content for message in messages]
        self.assertLess(
            content[0].index("[mem:7]"), len(content[0]), "记忆要挂在主 system 里"
        )
        for index, message in enumerate(messages[:-1]):
            self.assertNotIn("当前时间", message.content, f"第 {index} 条不该带时间")
        self.assertIn("当前时间", messages[-1].content)


class HistoryTrimmingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = test_settings(Path(self._tmp.name))

    def test_groups_split_on_user_messages(self) -> None:
        history: list[Message] = []
        for index in range(3):
            history.extend(_tool_round(index))
        groups = split_into_groups(history)
        self.assertEqual(len(groups), 3)
        self.assertTrue(all(group[0].role == "user" for group in groups))

    def test_trim_never_orphans_a_tool_result(self) -> None:
        """任何预算下都不能出现"工具结果找不到它的调用者"——那会让 API 直接报错。"""
        history: list[Message] = []
        for index in range(6):
            history.extend(_tool_round(index))

        for budget in (10, 40, 80, 120, 200, 400, 10000):
            trimmed, _ = trim_history(history, budget)
            seen_calls: set[str] = set()
            for message in trimmed:
                if message.role == "assistant":
                    seen_calls.update(call.id for call in message.tool_calls)
                elif message.role == "tool":
                    self.assertIn(
                        message.tool_call_id,
                        seen_calls,
                        f"budget={budget} 时出现了孤立的 tool 消息",
                    )

    def test_trim_always_keeps_latest_group(self) -> None:
        history: list[Message] = []
        for index in range(4):
            history.extend(_tool_round(index))
        trimmed, omitted = trim_history(history, budget_tokens=1)
        self.assertEqual(len(trimmed), 3)
        self.assertEqual(trimmed[0].role, "user")
        self.assertIn("第 3 个问题", trimmed[0].content)
        self.assertEqual(omitted, 3)

    def test_trim_drops_whole_groups_only(self) -> None:
        history: list[Message] = []
        for index in range(4):
            history.extend(_tool_round(index))
        trimmed, omitted = trim_history(history, budget_tokens=200)
        self.assertEqual(omitted, 4 - len(split_into_groups(trimmed)))

    def test_build_messages_puts_system_first(self) -> None:
        history = _tool_round(1)
        messages, omitted = build_messages(self.settings, history, "(空)")
        self.assertEqual(messages[0].role, "system")
        self.assertEqual(omitted, 0)
        # 主 system + 历史 + 末尾的时间块
        self.assertEqual(len(messages), len(history) + 2)
        self.assertIn("当前时间", messages[-1].content)

    # -- 缓存前缀：轮内与轮间 ----------------------------------------

    def test_first_step_is_a_prefix_of_the_second_step(self) -> None:
        """轮内多步：第 1 步的请求必须是第 2 步请求的前缀，第 2 步才全额命中缓存。

        "去掉末尾时间块再比"是必要的：时间块每步都在最后，而它后面的内容本来就是
        这个会话最新产生的，不涉及缓存损失（见 `build_tail_message`）。
        """
        step1_history = _tool_round(1)
        step1, _ = build_messages(self.settings, step1_history, "(核心)")
        step2_history = step1_history + [
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="c1", name="mem_create", arguments="{}")],
            ),
            Message(role="tool", tool_call_id="c1", name="mem_create", content="{}"),
        ]
        step2, _ = build_messages(self.settings, step2_history, "(核心)")

        self.assertLess(len(step1), len(step2))
        self.assertIn("当前时间", step1[-1].content)
        self.assertIn("当前时间", step2[-1].content)
        head = len(step1) - 1  # 去掉时间块
        self.assertEqual(
            [message.to_api() for message in step1[:head]],
            [message.to_api() for message in step2[:head]],
        )

    def test_consecutive_requests_share_everything_but_the_last_group(self) -> None:
        """缓存前提：本轮与上轮请求的公共前缀要覆盖**除最后一组外的全部历史**。

        检索块删掉（ADR-058）之后这条前缀只会更长 —— 但断言仍然要留着：
        它守的是"**没有任何易变内容被塞进历史中间**"。哪天有人把时间、检索结果
        或别的每轮都变的东西插进历史里，这条会立刻红。
        """
        g1, g2, g3 = _tool_round(1), _tool_round(2), _tool_round(3)
        previous, _ = build_messages(self.settings, g1 + g2, "(核心)")
        current, _ = build_messages(self.settings, g1 + g2 + g3, "(核心)")

        # 上轮 = [system] + g1 + g2 + [时间块]；公共前缀 = 去掉末尾时间块的那一段
        shared = len(previous) - 1
        self.assertGreater(shared, 1, "公共前缀退化成只剩 system 是严重退化")
        self.assertEqual(
            [message.to_api() for message in previous[:shared]],
            [message.to_api() for message in current[:shared]],
        )
        # 上轮在 shared 位置上是时间块；本轮在同一位置上是**新一组的第一条**（新提问）
        self.assertIn("当前时间", previous[shared].content)
        self.assertEqual(current[shared].content, g3[0].content)

    def test_reasoning_content_is_serialized_for_the_api(self) -> None:
        payload = _tool_round(1)[1].to_api()
        self.assertEqual(payload["reasoning_content"], "这是要回传的思维链")
        self.assertIn("tool_calls", payload)


class TokenEstimateTests(unittest.TestCase):
    def test_cjk_costs_more_than_latin(self) -> None:
        self.assertGreater(estimate_tokens("中文" * 50), estimate_tokens("ab" * 50))

    def test_empty_is_zero(self) -> None:
        self.assertEqual(estimate_tokens(""), 0)


if __name__ == "__main__":
    unittest.main()
