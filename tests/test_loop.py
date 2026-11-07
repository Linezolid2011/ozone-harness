"""Agent 循环测试：正常流程、四道熔断、思维链不回传、日志可重放。

全部基于 FakeProvider，不访问网络。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Iterator, Sequence

from harness.events import EventLog
from harness.llm_types import Finished, Message, StreamEvent, TextDelta, ToolCall
from harness.providers.base import ProviderError
from harness.tools.registry import ToolResult

from .helpers import Harness, make_harness, test_settings, text_reply, tool_reply, usage


class ExplodingProvider:
    """一调用就报错，用于验证后端故障被优雅处理而不是崩掉整个 CLI。"""

    name = "exploding"

    def stream(self, *args: Any, **kwargs: Any) -> Iterator[StreamEvent]:
        raise ProviderError("模拟后端故障：连接被重置")
        yield Finished()  # pragma: no cover - 让函数成为生成器


class LoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        # 注册得最早 → 最后执行：确保所有文件句柄都关了再删目录
        # （Windows 下删除被占用的文件会直接报 WinError 32）
        self.addCleanup(self._tmp.cleanup)

    def harness(self, script: Sequence[Sequence[StreamEvent]], **overrides: Any) -> Harness:
        """装配测试用 harness，并在用例结束时自动收尾（避免断言时连接已关闭）。"""
        built = make_harness(self.tmp, script, **overrides)
        self.addCleanup(built.close)
        return built

    # -- 正常路径 ----------------------------------------------------

    def test_single_step_text_turn(self) -> None:
        harness = self.harness([text_reply("你好呀", reasoning="想想要不要打招呼")])
        outcome = harness.loop.run("你好")

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.text, "你好呀")
        self.assertEqual(outcome.steps, 1)
        self.assertEqual(outcome.usage.total_tokens, 30)
        self.assertEqual(len(harness.provider.calls), 1)

        tool_names = [spec["function"]["name"] for spec in harness.provider.calls[0]["tools"]]
        self.assertEqual(tool_names, ["mem_create", "mem_del", "mem_edit"])

        kinds = [kind for kind, _ in harness.events]
        self.assertIn("reasoning", kinds)
        self.assertIn("text", kinds)
        self.assertIn("prompt", kinds)
        self.assertIn("turn_end", kinds)

    def test_tool_call_round_trip_writes_memory(self) -> None:
        script = [
            tool_reply(
                [("call_1", "mem_create", '{"content":"用户喜欢美式咖啡","category":"偏好"}')]
            ),
            text_reply("好的，记住了。"),
            text_reply("你喜欢美式咖啡。"),
        ]
        harness = self.harness(script)
        first = harness.loop.run("记住我喜欢美式咖啡")
        second = harness.loop.run("我喜欢什么？")

        self.assertEqual(first.text, "好的，记住了。")
        self.assertEqual(first.steps, 2)
        self.assertEqual(harness.memory.stats()["active"], 1)

        # 记忆快照在整轮内固定：system 消息字节一致，第 2 步才能全额命中前缀缓存
        step1_system = harness.provider.calls[0]["messages"][0].content
        step2_system = harness.provider.calls[1]["messages"][0].content
        self.assertEqual(step1_system, step2_system)
        self.assertNotIn("用户喜欢美式咖啡", step2_system)

        # 轮内缓存：第 1 步的 prompt（去掉末尾那个每步都变的时间块）必须是第 2 步的前缀。
        # 时间块放在最后正是为了这个 —— 放 system 里的话，跨分钟就会作废整段历史。
        step1 = harness.provider.calls[0]["payloads"]
        step2 = harness.provider.calls[1]["payloads"]
        self.assertIn("当前时间", step1[-1]["content"])
        self.assertIn("当前时间", step2[-1]["content"])
        self.assertEqual(step1[:-1], step2[: len(step1) - 1])

        # 写入结果通过工具返回值告知模型，而不是靠记忆块再重复一遍
        second_roles = [payload["role"] for payload in harness.provider.calls[1]["payloads"]]
        self.assertEqual(second_roles, ["system", "user", "assistant", "tool", "system"])
        tool_payload = next(
            payload
            for payload in harness.provider.calls[1]["payloads"]
            if payload["role"] == "tool"
        )
        self.assertIn("用户喜欢美式咖啡", tool_payload["content"])

        # 下一轮开始时，新记忆才出现在记忆块里
        next_turn_system = harness.provider.calls[2]["messages"][0].content
        self.assertIn("用户喜欢美式咖啡", next_turn_system)
        self.assertIn("[mem:1]", next_turn_system)
        self.assertEqual(second.text, "你喜欢美式咖啡。")

    def test_tool_call_id_is_matched_in_history(self) -> None:
        script = [
            tool_reply([("call_abc", "mem_create", '{"content":"一条记忆"}')]),
            text_reply("好"),
        ]
        harness = self.harness(script)
        harness.loop.run("记住一条")

        payloads = harness.provider.calls[1]["payloads"]
        assistant = next(p for p in payloads if p["role"] == "assistant")
        tool_message = next(p for p in payloads if p["role"] == "tool")
        self.assertEqual(assistant["tool_calls"][0]["id"], "call_abc")
        self.assertEqual(tool_message["tool_call_id"], "call_abc")
        self.assertEqual(tool_message["name"], "mem_create")

    def test_reasoning_content_is_sent_back_but_only_on_assistant(self) -> None:
        """官方要求：携带 tools 的请求，历史 assistant 的 reasoning_content 必须回传。

        之前我们按"思维链不该污染上下文"的直觉把它剥掉了 —— 那是错的：
        文档明确说漏传会 400，而且它会被拼进上下文，让模型在工具循环中保持思考连续性。
        """
        script = [
            tool_reply(
                [("call_1", "mem_create", '{"content":"记忆"}')],
                text="我先记一下。",
                reasoning="第一步的思考",
            ),
            text_reply("done", reasoning="第二次思考"),
        ]
        harness = self.harness(script)
        harness.loop.run("记住")

        second = harness.provider.calls[1]["payloads"]
        assistant = next(payload for payload in second if payload["role"] == "assistant")
        self.assertEqual(assistant["reasoning_content"], "第一步的思考")

        # 思维链只能挂在 assistant 上，不能污染 system / user / tool
        for payload in second:
            if payload["role"] != "assistant":
                self.assertNotIn("reasoning_content", payload)

        # 本地日志同样要留全，用于复盘
        assistant_events = [
            record
            for record in harness.log.read_events()
            if record["kind"] == "assistant_message"
        ]
        self.assertEqual(assistant_events[1]["message"]["reasoning"], "第二次思考")

    def test_usage_accumulates_across_steps(self) -> None:
        script = [
            tool_reply([("c1", "mem_create", '{"content":"a"}')], use=usage(total=30)),
            tool_reply([("c2", "mem_create", '{"content":"b"}')], use=usage(total=50)),
            text_reply("完事", use=usage(total=20)),
        ]
        harness = self.harness(script)
        outcome = harness.loop.run("连记两条")

        self.assertEqual(outcome.steps, 3)
        self.assertEqual(outcome.usage.total_tokens, 100)

    # -- 熔断 --------------------------------------------------------

    def test_max_steps_breaker(self) -> None:
        script = [
            tool_reply([("c1", "mem_create", '{"content":"记忆一"}')]),
            tool_reply([("c2", "mem_create", '{"content":"记忆二"}')]),
            tool_reply([("c3", "mem_create", '{"content":"记忆三"}')]),
        ]
        harness = self.harness(script, max_steps=3)
        outcome = harness.loop.run("一直记")

        self.assertEqual(outcome.stop_reason, "max_steps")
        self.assertEqual(outcome.steps, 3)
        self.assertFalse(outcome.ok)

    def test_repeated_tool_call_breaker(self) -> None:
        same = '{"content":"永远同一条"}'
        script = [tool_reply([(f"c{index}", "mem_create", same)]) for index in range(6)]
        harness = self.harness(script, max_steps=8)
        outcome = harness.loop.run("死循环测试")

        self.assertEqual(outcome.stop_reason, "repeated_tool_calls")
        self.assertEqual(outcome.steps, 3)
        # 第三次重复的调用被拦在执行之前，所以只写入了一条记忆
        self.assertEqual(harness.memory.stats()["active"], 1)

    def test_token_budget_breaker(self) -> None:
        script = [
            tool_reply([("c1", "mem_create", '{"content":"x"}')], use=usage(total=30)),
            text_reply("不该走到这里"),
        ]
        harness = self.harness(script, max_tokens_per_turn=25)
        outcome = harness.loop.run("超预算")

        self.assertEqual(outcome.stop_reason, "token_budget")
        self.assertEqual(outcome.steps, 1, "预算熔断时不应把未执行的迭代算进步数")
        self.assertEqual(len(harness.provider.calls), 1)

    def test_token_budget_breaker_stops_at_the_exact_cap(self) -> None:
        """达到上限就已经没有下一次调用的预算，不是必须超过才停。"""
        script = [
            tool_reply([("c1", "mem_create", '{"content":"x"}')], use=usage(total=30)),
            text_reply("不该走到这里"),
        ]
        harness = self.harness(script, max_tokens_per_turn=30)

        outcome = harness.loop.run("刚好到上限")

        self.assertEqual(outcome.stop_reason, "token_budget")
        self.assertEqual(len(harness.provider.calls), 1)

    def test_wall_clock_breaker(self) -> None:
        script = [tool_reply([("c1", "mem_create", '{"content":"x"}')]) for _ in range(4)]
        harness = self.harness(script, max_wall_seconds=0.0)
        outcome = harness.loop.run("立刻超时")

        self.assertEqual(outcome.stop_reason, "wall_clock")
        self.assertEqual(outcome.steps, 0)

    def test_provider_error_is_surfaced_not_raised(self) -> None:
        built = Harness(test_settings(self.tmp), ExplodingProvider())  # type: ignore[arg-type]
        self.addCleanup(built.close)
        outcome = built.loop.run("后端故障")

        self.assertEqual(outcome.stop_reason, "error")
        self.assertIn("模拟后端故障", outcome.error or "")
        kinds = [record["kind"] for record in built.log.read_events()]
        self.assertIn("error", kinds)

    # -- 日志与重放 --------------------------------------------------

    def test_conversation_round_trips_through_the_log(self) -> None:
        script = [
            tool_reply([("c1", "mem_create", '{"content":"一条记忆"}')]),
            text_reply("记住了"),
        ]
        harness = self.harness(script)
        harness.loop.run("记住一条")
        harness.loop.run("你好")
        live = harness.log.conversation()
        path = harness.log.path

        reopened = EventLog(path, "test-session")
        self.addCleanup(reopened.close)
        replayed = reopened.conversation()

        self.assertEqual([m.role for m in live], [m.role for m in replayed])
        self.assertEqual([m.content for m in live], [m.content for m in replayed])
        self.assertEqual(
            [m.role for m in replayed],
            ["user", "assistant", "tool", "assistant", "user", "assistant"],
        )
        # 重放出的 assistant 消息里必须带着 tool_calls，重建上下文才不会断链
        self.assertEqual(replayed[1].tool_calls[0].name, "mem_create")
        self.assertEqual(replayed[2].tool_call_id, "c1")

    def test_append_after_an_unterminated_line_preserves_the_new_event(self) -> None:
        """半行或完整的无换行末尾，都不能把下一条正常事件粘成坏 JSON。"""
        for name, tail, expected in (
            ("partial", '{"seq": 2, "kind": "user_mess', ["已有", "新事件"]),
            (
                "complete",
                '{"seq": 2, "kind": "user_message", "text": "完整但没换行"}',
                ["已有", "完整但没换行", "新事件"],
            ),
        ):
            with self.subTest(name=name):
                path = self.tmp / "sessions" / f"{name}.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    '{"seq": 1, "kind": "user_message", "text": "已有"}\n' + tail,
                    encoding="utf-8",
                )

                log = EventLog(path, name)
                try:
                    appended = log.append("user_message", text="新事件")
                finally:
                    log.close()

                self.assertEqual(appended["seq"], 3, "末尾一行也占过序号，不能复用")
                events = log.read_events()
                self.assertEqual([event["text"] for event in events], expected)

    def test_concurrent_handles_preserve_seq_order_in_the_file(self) -> None:
        """序号 1 已分配但尚未落盘时，序号 2 不能抢先写入。"""
        path = self.tmp / "sessions" / "ordered.jsonl"
        first = EventLog(path, "ordered")
        second = EventLog(path, "ordered")
        entered = threading.Event()
        release = threading.Event()
        second_attempt = threading.Event()
        second_finished = threading.Event()

        class BlockingWriter:
            def __init__(self, inner: Any) -> None:
                self.inner = inner

            def write(self, text: str) -> int:
                entered.set()
                release.wait(5)
                return self.inner.write(text)

            def __getattr__(self, name: str) -> Any:
                return getattr(self.inner, name)

        first._handle = BlockingWriter(first._handle)  # type: ignore[assignment]

        def append_second() -> None:
            second_attempt.set()
            second.append("session_title", title="后分配")
            second_finished.set()

        first_thread = threading.Thread(target=first.append, args=("user_message",), kwargs={"text": "先分配"})
        second_thread = threading.Thread(target=append_second)
        try:
            first_thread.start()
            self.assertTrue(entered.wait(5))
            second_thread.start()
            self.assertTrue(second_attempt.wait(5))
            self.assertFalse(
                second_finished.wait(0.1),
                "第二个句柄越过了尚未落盘的较小 seq",
            )
        finally:
            release.set()
            first_thread.join(5)
            second_thread.join(5)
            first.close()
            second.close()

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual([event["seq"] for event in first.read_events()], [1, 2])

    def test_memory_is_fully_injected_at_any_size(self) -> None:
        """**任何规模都全量注入**（ADR-058）：条数上限就是那道闸。

        以前这里分两条用例（小库全量 / 大库切成"核心 + 检索"）。检索机制删掉后，
        两者是同一件事，所以合成一条并**把两个规模都跑一遍** —— 顺便守住
        "请求结构没有被塞进额外的东西"：永远只有 [system, 提问, 时间块]。
        """
        for size in (2, 30):
            with self.subTest(size=size):
                # **每个规模用一个全新的临时数据目录**：两个 harness 共用一个目录时，
                # 它们会共用同一个会话日志（`test-session.jsonl`），于是第二个规模
                # 的历史里会带着第一个规模的对话 —— 断言就成了"在测上一条用例的残留"。
                with tempfile.TemporaryDirectory() as tmp:
                    harness = make_harness(Path(tmp), [text_reply("好")])
                    try:
                        for index in range(size):
                            harness.memory.create(f"事实编号 {index}", "偏好")

                        harness.loop.run("我有什么偏好？")

                        payloads = harness.provider.calls[0]["payloads"]
                        self.assertEqual(
                            [payload["role"] for payload in payloads],
                            ["system", "user", "system"],
                            "全量注入下不该有任何额外的注入块",
                        )
                        self.assertNotIn("本轮相关记忆", payloads[0]["content"])
                        for index in (0, size // 2, size - 1):
                            self.assertIn(f"事实编号 {index}", payloads[0]["content"])

                        prompt = harness.log.prompts()[0]
                        self.assertEqual(prompt["memory_count"], size)
                        self.assertEqual(prompt["memory_limit"], 50)
                        # 界面上的数字与实际注入必须来自同一个判断点
                        self.assertEqual(
                            prompt["memory_text"].splitlines()[0], f"（已用 {size}/50）"
                        )

                        # 召回事件已经不存在了；取而代之的是 memory 事件
                        self.assertEqual(
                            [kind for kind, _ in harness.events if kind == "recall"], []
                        )
                        memory_events = [
                            data for kind, data in harness.events if kind == "memory"
                        ]
                        self.assertEqual(len(memory_events), 1, "一轮只报一次记忆用量")
                        self.assertEqual(memory_events[0]["count"], size)
                        self.assertEqual(memory_events[0]["limit"], 50)
                        self.assertEqual(memory_events[0]["omitted"], 0)
                    finally:
                        harness.close()

    def test_prompt_events_record_the_exact_request(self) -> None:
        harness = self.harness([text_reply("嗨")])
        harness.loop.run("你好")

        prompts = harness.log.prompts()
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["messages"][0]["role"], "system")
        # 倒数第二条是用户输入，最后一条是每轮都变的时间块
        self.assertEqual(prompts[0]["messages"][-2]["content"], "你好")
        self.assertIn("当前时间", prompts[0]["messages"][-1]["content"])
        self.assertGreater(prompts[0]["estimated_tokens"], 0)

    def test_truncated_output_is_not_reported_as_completed(self) -> None:
        """finish_reason=length 必须显式暴露。

        否则"模型想到一半被 max_tokens 砍断"会伪装成本轮正常完成 ——
        实测踩过：dream 整理 80 条记忆时 reasoning 用满输出预算、零工具调用，
        却被报告成 stop_reason=completed。
        """
        script = [[TextDelta(text="想到一半"), Finished(reason="length", usage=usage())]]
        harness = self.harness(script)
        outcome = harness.loop.run("你好")

        self.assertEqual(outcome.stop_reason, "output_truncated")
        self.assertFalse(outcome.ok)
        notices = [
            record
            for record in harness.log.read_events()
            if record["kind"] == "notice"
        ]
        self.assertTrue(any("截断" in record["text"] for record in notices))

    def test_missing_finish_reason_still_completes(self) -> None:
        """后端偶发不给 finish_reason 时，不应把整轮判成失败。"""
        script = [[TextDelta(text="只有正文"), Finished(reason=None, usage=usage())]]
        harness = self.harness(script)
        outcome = harness.loop.run("你好")

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.text, "只有正文")


def _dangling_tool_call_ids(payloads: list[dict[str, Any]]) -> list[str]:
    """找出"有 tool_calls 却找不到配对 tool 结果"的调用 id。

    这种请求 API 会直接 400，所以任何构造出来的 prompt 都该先过这一关。
    """
    answered = {
        payload.get("tool_call_id")
        for payload in payloads
        if payload.get("role") == "tool"
    }
    dangling: list[str] = []
    for payload in payloads:
        if payload.get("role") != "assistant":
            continue
        for call in payload.get("tool_calls") or []:
            if call.get("id") not in answered:
                dangling.append(str(call.get("id")))
    return dangling


class AlwaysFailsTool:
    """总是失败的工具，用来验证"连续失败"熔断（测试用，不联网）。"""

    name = "always_fails"
    description = "总是失败"
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    def run(self, args: dict[str, Any]) -> ToolResult:
        return ToolResult(ok=False, payload={"error": "打不开"}, display="打不开")


class ToolCallIntegrityTests(unittest.TestCase):
    """tool_calls 与 tool 结果必须成对出现。

    不配对的后果不是"看着别扭"，而是协议层直接 400，而且那个会话会一直坏下去
    （裁剪和压缩都不会碰它）。这里守住两条路：熔断/中止时补桩，
    以及重放层对历史上已经写坏的日志兜底修补。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def harness(
        self, script: Sequence[Sequence[StreamEvent]], **overrides: Any
    ) -> Harness:
        built = make_harness(self.tmp, script, **overrides)
        self.addCleanup(built.close)
        return built

    def test_repeat_breaker_leaves_no_dangling_tool_calls(self) -> None:
        calls = [("c1", "mem_create", '{"content":"用户喜欢美式"}')]
        script = [tool_reply(calls) for _ in range(4)] + [text_reply("好")]
        harness = self.harness(script, max_steps=5)

        first = harness.loop.run("记住我喜欢美式")
        self.assertEqual(first.stop_reason, "repeated_tool_calls")

        skipped = [
            record
            for record in harness.log.read_events()
            if record.get("kind") == "tool_call" and record.get("skipped")
        ]
        self.assertEqual(len(skipped), 1, "被熔断的那次调用必须补上一条结果")

        harness.loop.run("继续")
        self.assertEqual(
            _dangling_tool_call_ids(harness.provider.calls[-1]["payloads"]), []
        )

    def test_abort_mid_tools_leaves_no_dangling_tool_calls(self) -> None:
        """客户端在工具执行途中断开（点了"停止"）也不能留下悬空的 tool_calls。"""

        class Boom:
            name = "boom"
            description = "模拟客户端断开"
            parameters: dict[str, Any] = {"type": "object", "properties": {}}

            def __init__(self) -> None:
                self.calls = 0

            def run(self, args: dict[str, Any]) -> ToolResult:
                self.calls += 1
                raise KeyboardInterrupt("模拟用户点了停止")

        harness = self.harness(
            [tool_reply([("c1", "boom", "{}"), ("c2", "mem_create", '{"content":"x"}')])],
            max_steps=3,
        )
        harness.registry.register(Boom())
        # 循环会记完 turn_end 再把这个异常抛出去（进程级别中断不该被吞掉）
        with self.assertRaises(KeyboardInterrupt):
            harness.loop.run("随便")

        events = harness.log.read_events()
        self.assertEqual(events[-1]["kind"], "turn_end", "中止也必须留下 turn_end")
        self.assertEqual(
            _dangling_tool_call_ids(
                [message.to_api() for message in harness.log.conversation()]
            ),
            [],
        )

    def test_disconnect_after_a_real_tool_result_does_not_duplicate_it(self) -> None:
        """结果已经落盘、仅 UI 发送失败时，不能再给同一个调用补一条失败结果。"""
        harness = self.harness(
            [
                tool_reply(
                    [
                        ("c1", "mem_create", '{"content":"一"}'),
                        ("c2", "mem_create", '{"content":"二"}'),
                    ]
                )
            ]
        )

        def disconnect(kind: str, **data: Any) -> None:
            if kind == "tool_result":
                raise KeyboardInterrupt("模拟客户端在结果帧断开")

        harness.loop.emit = disconnect
        with self.assertRaises(KeyboardInterrupt):
            harness.loop.run("记两条")

        results = [
            record
            for record in harness.log.read_events()
            if record.get("kind") == "tool_call"
        ]
        by_id: dict[str, list[dict[str, Any]]] = {}
        for record in results:
            by_id.setdefault(str(record["tool_call_id"]), []).append(record)
        self.assertEqual(len(by_id["c1"]), 1, "真实结果已经落盘，不许再补桩")
        self.assertTrue(by_id["c1"][0]["ok"])
        self.assertEqual(len(by_id["c2"]), 1, "未执行的第二条仍要补桩")
        self.assertTrue(by_id["c2"][0]["skipped"])

    def test_replay_repairs_a_log_that_was_already_broken(self) -> None:
        """历史里已经写坏的日志（旧版本留下的悬空 tool_calls）在重放时被补桩。"""
        harness = self.harness([text_reply("好")])
        harness.log.append("user_message", text="第一问")
        harness.log.append(
            "assistant_message",
            step=1,
            finish_reason="tool_calls",
            usage=usage().to_dict(),
            message=Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="lost", name="mem_create", arguments="{}")],
            ).to_record(),
        )
        harness.log.append("user_message", text="第二问")

        messages = harness.log.conversation()
        self.assertEqual([message.role for message in messages], ["user", "assistant", "tool", "user"])
        self.assertEqual(messages[2].tool_call_id, "lost")
        self.assertFalse(json.loads(messages[2].content)["ok"])
        self.assertEqual(
            _dangling_tool_call_ids([message.to_api() for message in messages]), []
        )

    def test_consecutive_tool_failures_are_counted_across_steps(self) -> None:
        """第五道熔断必须**跨步累计**。

        失败通常是一步一次（看到失败 → 换一个地址再试）。每步清零的话，
        "连续 4 步各失败一次"永远触发不了 —— 而那正是它为之而生的场景
        （实测一轮连开 4 个打不开的地址、烧光 8 步、耗掉 54k token）。
        """
        harness = self.harness(
            [
                # 每步换一个地址：这既符合真实翻车场景（换了 URL 再试），
                # 也避免先撞上"重复调用"那道熔断
                tool_reply(
                    [(f"c{index}", "always_fails", json.dumps({"url": f"https://e/{index}"}))]
                )
                for index in range(4)
            ]
            + [text_reply("好吧")],
            max_tool_failures=3,
        )
        harness.registry.register(AlwaysFailsTool())

        outcome = harness.loop.run("试试能不能打开")
        self.assertEqual(outcome.stop_reason, "tool_failures")


if __name__ == "__main__":
    unittest.main()
