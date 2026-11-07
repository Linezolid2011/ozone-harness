"""自动 compact 测试（M6）。

要验证的核心命题有两层：
  1. 历史接近预算上限时，较早的对话被**压成摘要**而不是被静默丢弃；
  2. **在哪里触发取决于本轮选中的模型** —— 32K 窗口的模型必须比 1M 的更早动手。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.config import Settings
from harness.context import build_messages, render_for_summary, split_into_groups
from harness.events import last_summary, last_summary_mark
from harness.llm_types import Finished, Message, TextDelta, ToolCall, estimate_messages_tokens

from .helpers import Harness, make_harness, test_settings, text_reply, tool_reply, usage

LONG = "这是一段用来把上下文撑大的内容。" * 60  # 约 900 字 ≈ 900 token


def long_turn(index: int) -> list[Message]:
    return [
        Message(role="user", content=f"第 {index} 段：{LONG}"),
        Message(role="assistant", content="好的，我记下了。"),
    ]


class HistoryBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_budget_is_the_smaller_of_cost_cap_and_model_window(self) -> None:
        # 1M 窗口 + 成本上限 24000 → 成本上限说话
        wide = test_settings(self.tmp, context_token_budget=24_000, context_window=1_000_000)
        self.assertEqual(wide.history_budget(), 24_000)

        # 窗口只有 12K → 窗口说话（要留出输出、安全余量与固定开销）
        narrow = test_settings(self.tmp, context_token_budget=24_000, context_window=12_000)
        self.assertLess(narrow.history_budget(), 12_000)
        self.assertEqual(
            narrow.history_budget(),
            12_000 - narrow.max_output_tokens - narrow.context_margin(),
            "没有工具/system 开销时，窗口那一半就是窗口减输出再减余量（ADR-100）",
        )

    def test_model_window_is_never_ignored_for_cost_savings(self) -> None:
        """成本上限放宽到很大时，仍不能超过模型窗口 —— 否则会直接 400。"""
        settings = test_settings(self.tmp, context_token_budget=1_000_000, context_window=32_000)
        self.assertLessEqual(
            settings.history_budget()
            + settings.max_output_tokens
            + settings.context_margin(),
            32_000,
        )

    def test_fixed_overhead_is_subtracted_from_the_window(self) -> None:
        """工具/system/记忆/摘要这些固定开销必须先扣掉（ADR-100）。

        旧算法把它们当免费的：窗口 32K、历史预算 24K，看着没超，
        但工具 + system + 记忆 + README 又能有一万 —— 请求就出去了。
        """
        settings = test_settings(
            self.tmp, context_token_budget=1_000_000, context_window=32_000
        )
        overhead = 5_000
        self.assertEqual(
            settings.history_budget() - settings.history_budget(overhead_tokens=overhead),
            overhead,
        )

    def test_budget_is_never_negative(self) -> None:
        """窗口比固定开销还小时返回 0，而不是负数（负数会让裁剪逻辑发疯）。"""
        settings = test_settings(
            self.tmp, context_token_budget=1_000_000, context_window=2_000
        )
        self.assertEqual(settings.history_budget(overhead_tokens=10_000), 0)

    def test_safety_margin_can_be_disabled_and_is_capped_by_a_small_window(self) -> None:
        off = test_settings(self.tmp, context_safety_tokens=0, context_window=32_000)
        self.assertEqual(off.context_margin(), 0)

        tiny = test_settings(self.tmp, context_safety_tokens=10_000, context_window=4_000)
        self.assertEqual(tiny.context_margin(), 200, "小窗口按 5% 收紧，不能被余量吃光")


class CompactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _harness(self, script, **overrides) -> Harness:
        """装一个先把历史撑大的 harness。"""
        harness = make_harness(self.tmp, script, **overrides)
        self.addCleanup(harness.close)
        return harness

    def _fill_history(self, harness: Harness, turns: int = 8) -> None:
        """直接往日志里塞历史（比真跑 8 轮便宜得多）。"""
        for index in range(turns):
            harness.log.append("user_message", text=f"第 {index} 段：{LONG}")
            harness.log.append(
                "assistant_message",
                step=1,
                finish_reason="stop",
                usage=usage(total=10).to_dict(),
                message=Message(role="assistant", content="好的，我记下了。").to_record(),
            )

    def test_does_not_compact_when_under_budget(self) -> None:
        harness = self._harness([text_reply("正常回答")])
        harness.log.append("user_message", text="很短的一句话")
        harness.loop.run("再短一句")
        self.assertEqual(last_summary(harness.log.path), ("", 0))
        self.assertEqual(len(harness.provider.calls), 1, "不该多出一次摘要调用")

    def test_compacts_when_over_threshold(self) -> None:
        script = [text_reply("摘要内容：用户聊了几段占位文本。"), text_reply("好的")]
        harness = self._harness(script, context_token_budget=6000, compact_threshold=0.75)
        self._fill_history(harness)

        harness.loop.run("现在呢")

        summary, covered = last_summary(harness.log.path)
        self.assertTrue(summary)
        self.assertGreater(covered, 0, "必须记录覆盖了多少条消息")

        # 第一次调用是摘要（不带工具），第二次才是正常对话
        self.assertEqual(harness.provider.calls[0]["tools"], [])
        self.assertEqual(
            harness.provider.calls[0]["max_tokens"], harness.settings.compact_max_output_tokens
        )
        self.assertTrue(harness.provider.calls[1]["tools"], "对话那一步必须带工具")

    def test_covered_messages_no_longer_enter_the_context(self) -> None:
        script = [text_reply("摘要：早前聊过很多占位文本。"), text_reply("好的")]
        harness = self._harness(script, context_token_budget=6000)
        self._fill_history(harness, turns=8)

        harness.loop.run("现在呢")

        summary, history, mark = harness.log.compacted_context()
        self.assertTrue(summary)
        self.assertGreater(mark.messages, 0)
        self.assertGreater(mark.groups, 0, "水位线要以**组数**为准")
        all_messages = harness.log.conversation()
        self.assertEqual(len(history), len(all_messages) - mark.messages)
        self.assertEqual(history[0].role, "user", "切点必须落在组的开头")

        # 被覆盖的是最早那几段，它们不该再出现在上下文里；
        # 最近那几段必须留着（每段用的是同一句填充文本，所以要按段号判断）
        joined = "".join(m.content for m in history)
        self.assertNotIn("第 0 段", joined)
        self.assertIn("第 7 段", joined)

    def test_summary_is_injected_as_a_system_message_right_after_the_main_one(self) -> None:
        script = [text_reply("摘要正文"), text_reply("好的")]
        harness = self._harness(script, context_token_budget=6000)
        self._fill_history(harness)
        harness.loop.run("现在呢")

        summary, history, _covered = harness.log.compacted_context()
        messages, _ = build_messages(
            harness.settings, history, "(核心)", summary_text=summary
        )
        self.assertEqual(messages[0].role, "system")
        self.assertEqual(messages[1].role, "system")
        self.assertIn("摘要正文", messages[1].content)
        self.assertIn("更早对话的摘要", messages[1].content)

    def test_trigger_point_depends_on_the_selected_model(self) -> None:
        """用户这轮选了什么模型，决定 compact 在哪触发。"""
        script = [text_reply("摘要"), text_reply("好的")]
        # 成本上限放到很大，让"模型窗口"成为唯一的约束
        wide = self._harness(
            list(script),
            context_token_budget=1_000_000,
            context_window=1_000_000,
        )
        self._fill_history(wide, turns=3)   # 约 3000 token
        wide.loop.run("现在呢")
        self.assertEqual(last_summary(wide.log.path)[0], "", "1M 窗口下不该触发")

        narrow = self._harness(
            list(script),
            context_token_budget=1_000_000,
            # 窗口要小到"扣掉工具/system 之后"只剩两三千：ADR-100 之后
            # 固定开销也算在里面，所以触发得比旧算法更早、也更靠真实窗口。
            context_window=6_000,
        )
        self._fill_history(narrow, turns=4)   # 约 3600 token
        narrow.loop.run("现在呢")
        self.assertTrue(
            last_summary(narrow.log.path)[0], "6K 窗口下必须更早触发"
        )

    def test_two_compactions_accumulate_the_covered_count(self) -> None:
        script = [text_reply("第一次摘要"), text_reply("好的"), text_reply("第二次摘要"), text_reply("好的")]
        harness = self._harness(script, context_token_budget=6000)
        self._fill_history(harness, turns=8)
        harness.loop.run("第一问")
        first_summary, first_covered = last_summary(harness.log.path)
        self.assertTrue(first_summary)

        self._fill_history(harness, turns=8)
        harness.loop.run("第二问")
        second_summary, second_covered = last_summary(harness.log.path)
        self.assertEqual(second_summary, "第二次摘要")
        self.assertGreater(second_covered, first_covered, "累计覆盖数必须递增")

    def test_legacy_message_watermark_upgrades_to_absolute_group_coverage(self) -> None:
        """老日志的条数水位线不能让下一轮压缩从第 0 组重新计数。"""
        harness = self._harness(
            [text_reply("新摘要"), text_reply("好")], context_token_budget=2000
        )
        for index in range(2):
            harness.log.append("user_message", text=f"G{index}：{LONG}")
            harness.log.append(
                "assistant_message",
                message=Message(role="assistant", content="旧回答").to_record(),
                usage=usage().to_dict(),
            )
        harness.log.append(
            "session_summary", summary="旧摘要", covered_messages=2
        )
        harness.log.append("user_message", text=f"G2：{LONG}")
        harness.log.append(
            "assistant_message",
            message=Message(role="assistant", content="旧回答").to_record(),
            usage=usage().to_dict(),
        )

        harness.loop.run("当前问题")

        _summary, mark = last_summary_mark(harness.log.path)
        self.assertEqual(mark.groups, 3, "G0 旧覆盖 + 本次折叠 G1/G2")
        _summary, history, _mark = harness.log.compacted_context()
        self.assertEqual(
            [message.content for message in history if message.role == "user"],
            ["当前问题"],
            "刚折叠的 G2 不能因为水位线少算一组又回到上下文",
        )

    def test_disabled_by_config(self) -> None:
        harness = self._harness([text_reply("好的")], context_token_budget=6000, compact_enabled=False)
        self._fill_history(harness)
        harness.loop.run("现在呢")
        self.assertEqual(last_summary(harness.log.path), ("", 0))
        self.assertEqual(len(harness.provider.calls), 1, "关掉之后不该有摘要调用")

    def test_falls_back_to_dropping_when_the_summary_fails(self) -> None:
        """摘要调用失败/返回空时要退回旧行为，而不是把这一轮也搞崩。"""
        script = [[], text_reply("还是照常回答")]
        harness = self._harness(script, context_token_budget=6000)
        self._fill_history(harness)

        outcome = harness.loop.run("现在呢")

        self.assertEqual(last_summary(harness.log.path), ("", 0))
        self.assertTrue(outcome.ok, "摘要失败不能影响本轮对话")
        notices = [
            record for record in harness.log.read_events() if record["kind"] == "notice"
        ]
        self.assertTrue(any("压缩失败" in record["text"] for record in notices))

    def test_truncated_summary_is_never_persisted_as_complete(self) -> None:
        """`finish_reason=length` 的半份摘要不能推进水位线，否则未摘要的历史会丢失。"""
        script = [
            [TextDelta(text="半份摘要"), Finished(reason="length", usage=usage(total=500))],
            text_reply("正常回答"),
        ]
        harness = self._harness(script, context_token_budget=6000)
        self._fill_history(harness)

        outcome = harness.loop.run("现在呢")

        self.assertEqual(last_summary(harness.log.path), ("", 0))
        self.assertEqual(outcome.text, "正常回答")
        self.assertEqual(outcome.usage.total_tokens, 500 + 30, "失败的摘要调用也花了钱")

    def test_compact_cost_is_counted_into_the_turn(self) -> None:
        script = [
            text_reply("摘要", use=usage(total=500, prompt=400, completion=100)),
            text_reply("好的", use=usage(total=30, prompt=20, completion=10)),
        ]
        harness = self._harness(script, context_token_budget=6000)
        self._fill_history(harness)
        outcome = harness.loop.run("现在呢")
        # 530 = 压缩那次 500 + 对话那次 30
        self.assertEqual(outcome.usage.total_tokens, 530)

    def test_compact_cost_participates_in_the_token_breaker(self) -> None:
        """摘要先烧完单轮预算时，不能再发正常对话请求。"""
        harness = self._harness(
            [
                text_reply("摘要", use=usage(total=500, prompt=400, completion=100)),
                text_reply("不该发送"),
            ],
            context_token_budget=6000,
            max_tokens_per_turn=25,
        )
        self._fill_history(harness)

        outcome = harness.loop.run("现在呢")

        self.assertEqual(outcome.stop_reason, "token_budget")
        self.assertEqual(outcome.steps, 0)
        self.assertEqual(len(harness.provider.calls), 1, "只有摘要请求应该发出去")
        self.assertEqual(outcome.usage.total_tokens, 500)

    def test_single_group_is_never_compacted(self) -> None:
        """只剩当前这一组时可压的东西为零，必须安全返回。

        注意"当前这轮的提问"永远自成最后一组，所以只有**没有任何历史**时才只剩一组 ——
        这里用一条超长提问把估算顶过阈值来命中这条分支。
        """
        harness = self._harness([text_reply("好的")], context_token_budget=200)
        outcome = harness.loop.run(LONG)
        self.assertEqual(last_summary(harness.log.path), ("", 0))
        self.assertTrue(outcome.ok)


    def test_compaction_error_never_breaks_the_turn(self) -> None:
        """压缩只是优化：它抛任何异常都必须降级为"不压缩"。

        否则后果很难看：user_message 已经落盘、却没有 turn_end、用户等不到任何回答
        —— 相当于一轮对话凭空消失（这违背 ADR-006 对日志的承诺）。
        """
        harness = self._harness([text_reply("照常回答")], context_token_budget=6000)
        self._fill_history(harness)

        def boom() -> None:
            raise RuntimeError("压缩炸了")

        harness.log.compacted_context = boom  # type: ignore[method-assign]

        outcome = harness.loop.run("现在呢")

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.text, "照常回答")
        events = harness.log.read_events()
        self.assertEqual(events[-1]["kind"], "turn_end")
        self.assertTrue(
            any(
                "压缩阶段出错" in record["text"]
                for record in events
                if record["kind"] == "notice"
            ),
            "必须留下一条可读的 warn，而不是静默降级",
        )

    def test_abort_during_compaction_still_writes_turn_end(self) -> None:
        """主步循环尚未开始时停止，也不能留下一条看似永远运行中的回合。"""
        harness = self._harness(
            [text_reply("摘要"), text_reply("不该发送")], context_token_budget=6000
        )
        self._fill_history(harness)

        def abort_on_compact(kind: str, **_data: object) -> None:
            if kind == "compact":
                raise KeyboardInterrupt("模拟停止")

        harness.loop.emit = abort_on_compact
        with self.assertRaises(KeyboardInterrupt):
            harness.loop.run("现在呢")

        self.assertEqual(len(harness.provider.calls), 1, "只应发生摘要请求")
        event = harness.log.read_events()[-1]
        self.assertEqual(event["kind"], "turn_end")
        self.assertEqual(event["stop_reason"], "aborted")
        self.assertEqual(event["steps"], 0)
        self.assertEqual(event["usage"]["total_tokens"], 30, "摘要开销也要进入中止轮尾")

    def test_summary_input_is_never_truncated(self) -> None:
        """送进摘要的文本必须完整，且水位线只前移真正被摘要覆盖的那些组。

        按整段文本切尾会造成最坏的一种丢失：被切掉的部分既没进摘要、
        又被标成"已覆盖"，于是永久消失 —— 那正是这个功能要消灭的东西。
        """
        script = [text_reply("摘要"), text_reply("好")]
        harness = self._harness(
            script, context_token_budget=6000, compact_max_input_chars=1200
        )
        self._fill_history(harness, turns=8)

        harness.loop.run("现在呢")

        summary_prompt = harness.provider.calls[0]["messages"][-1].content
        self.assertNotIn("因过长被截断", summary_prompt, "送进摘要的文本不该被截断")

        _, mark = last_summary_mark(harness.log.path)
        self.assertGreater(mark.groups, 0, "水位线要按组记")
        self.assertGreater(mark.messages, 0)
        # 覆盖的消息数必须与"真正折叠的组"一致：1 组 = 2 条（user + assistant）
        self.assertEqual(mark.messages, mark.groups * 2)
        self.assertLessEqual(
            len(render_for_summary([], max_chars=None) or ""),
            harness.settings.compact_max_input_chars,
        )

    def test_watermark_beyond_the_log_still_keeps_the_current_question(self) -> None:
        """水位线比实际组数还大时（日志被手工改过之类），也不能把当前提问弄丢。"""
        harness = self._harness([text_reply("好")])
        harness.log.append("user_message", text="第一问")
        harness.log.append(
            "session_summary",
            summary="旧摘要",
            covered_groups=99,
            covered_messages=99,
        )
        harness.log.append("user_message", text="第二问")

        _summary, history, _mark = harness.log.compacted_context()
        self.assertTrue(history, "不能返回空历史")
        self.assertEqual(history[-1].content, "第二问", "当前提问必须在")

    def test_oversized_first_group_disables_compaction_instead_of_losing_it(self) -> None:
        """最早那组本身就超过摘要输入上限时，宁可不压缩（退回按组丢弃），也不截断。"""
        script = [text_reply("摘要"), text_reply("好")]
        harness = self._harness(
            script, context_token_budget=6000, compact_max_input_chars=50
        )
        self._fill_history(harness, turns=8)

        outcome = harness.loop.run("现在呢")

        self.assertTrue(outcome.ok)
        self.assertEqual(last_summary(harness.log.path), ("", 0), "不该写出摘要事件")
        notices = [
            record["text"]
            for record in harness.log.read_events()
            if record["kind"] == "notice"
        ]
        self.assertTrue(any("超过摘要输入上限" in text for text in notices))
    def test_render_for_summary_labels_every_role(self) -> None:
        text = render_for_summary(
            [
                Message(role="user", content="我喜欢红茶"),
                Message(role="assistant", content="记下了", tool_calls=[ToolCall("c1", "mem_create", "{}")]),
                Message(role="tool", tool_call_id="c1", name="mem_create", content='{"ok":true}'),
            ]
        )
        self.assertIn("用户：我喜欢红茶", text)
        self.assertIn("助手：记下了", text)
        self.assertIn("[调用 mem_create", text)
        self.assertIn("[工具结果 mem_create", text)

    def test_render_truncates_from_the_end(self) -> None:
        """超长时保留开头：较早的信息更需要被摘要固化。"""
        text = render_for_summary([Message(role="user", content="开" * 500)], max_chars=100)
        self.assertTrue(text.startswith("用户：开"))
        self.assertIn("截断", text)

    def test_group_split_is_what_compaction_uses(self) -> None:
        groups = split_into_groups(
            [Message(role="user", content="a"), Message(role="assistant", content="b"),
             Message(role="user", content="c")]
        )
        self.assertEqual(len(groups), 2)


if __name__ == "__main__":
    unittest.main()
