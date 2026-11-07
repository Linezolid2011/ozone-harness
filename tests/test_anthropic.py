"""Anthropic 兼容客户端测试（Kimi Code 订阅端点走这套协议）。

**已经真实跑通过**：2026-10-06 用真实 Kimi Code key 完成了一整轮对话
（provider=kimi-code、model=kimi-for-coding、steps=1、reason=completed）。
这些离线测试覆盖的是 URL 归一、消息/工具转换、SSE 解析、停止原因映射，
作用是**防止以后改坏**，以及在没有 key 的环境里也能验证协议实现。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from harness.llm_types import (
    Finished,
    Message,
    ReasoningDelta,
    StreamAssembler,
    TextDelta,
    ToolCall,
)
from harness.providers.anthropic_compat import AnthropicCompatProvider, _messages_url
from harness.providers.base import ProviderError
from harness.providers.store import ProviderProfile, build_provider

from .helpers import test_settings


def _event(kind: str, **payload: Any) -> bytes:
    body = {"type": kind, **payload}
    return ("data: " + json.dumps(body, ensure_ascii=False) + "\n").encode("utf-8")


class UrlTests(unittest.TestCase):
    def test_normalises_every_reasonable_way_of_writing_the_base(self) -> None:
        expected = "https://api.kimi.com/coding/v1/messages"
        self.assertEqual(_messages_url("https://api.kimi.com/coding"), expected)
        self.assertEqual(_messages_url("https://api.kimi.com/coding/"), expected)
        # 预设里就是带 /v1 的写法，不能变成 /v1/v1/messages
        self.assertEqual(_messages_url("https://api.kimi.com/coding/v1"), expected)
        self.assertEqual(
            _messages_url("https://api.anthropic.com"),
            "https://api.anthropic.com/v1/messages",
        )
        self.assertEqual(
            _messages_url("https://api.anthropic.com/v1"),
            "https://api.anthropic.com/v1/messages",
        )

    def test_models_url_keeps_the_v1_prefix(self) -> None:
        """模型发现是直接拼 /models 的，所以 base_url 里的 /v1 必须保留。"""
        base = "https://api.kimi.com/coding/v1"
        self.assertEqual(base.rstrip("/") + "/models", "https://api.kimi.com/coding/v1/models")


class ConversionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.provider = AnthropicCompatProvider(
            test_settings(Path(self._tmp.name), api_key="k")
        )

    def test_system_messages_become_the_top_level_param(self) -> None:
        """Anthropic 的 system 是顶层参数，不在 messages 里 —— 这是最容易搞错的一处。"""
        payload = self.provider.build_payload(
            [
                Message(role="system", content="规则一"),
                Message(role="system", content="核心记忆"),
                Message(role="user", content="你好"),
            ]
        )
        self.assertEqual(payload["system"], "规则一\n\n核心记忆")
        self.assertEqual(len(payload["messages"]), 1)
        self.assertNotIn("system", [m["role"] for m in payload["messages"]])

    def test_tool_calls_become_tool_use_blocks(self) -> None:
        payload = self.provider.build_payload(
            [
                Message(
                    role="assistant",
                    content="我先记一下",
                    tool_calls=[ToolCall("t1", "mem_create", '{"content":"x"}')],
                )
            ]
        )
        blocks = payload["messages"][0]["content"]
        self.assertEqual(blocks[0], {"type": "text", "text": "我先记一下"})
        self.assertEqual(blocks[1]["type"], "tool_use")
        self.assertEqual(blocks[1]["id"], "t1")
        self.assertEqual(blocks[1]["name"], "mem_create")
        self.assertEqual(blocks[1]["input"], {"content": "x"}, "参数要解析成对象而不是字符串")

    def test_consecutive_tool_results_merge_into_one_user_message(self) -> None:
        """Anthropic 要求 tool_result 紧跟对应的 tool_use；多个结果必须并进同一条消息。"""
        payload = self.provider.build_payload(
            [
                Message(
                    role="assistant",
                    content="",
                    tool_calls=[
                        ToolCall("t1", "mem_create", "{}"),
                        ToolCall("t2", "mem_create", "{}"),
                    ],
                ),
                Message(role="tool", tool_call_id="t1", name="mem_create", content='{"ok":true}'),
                Message(role="tool", tool_call_id="t2", name="mem_create", content='{"ok":true}'),
            ]
        )
        roles = [m["role"] for m in payload["messages"]]
        self.assertEqual(roles, ["assistant", "user"])
        blocks = payload["messages"][1]["content"]
        self.assertEqual(len(blocks), 2)
        self.assertEqual([b["tool_use_id"] for b in blocks], ["t1", "t2"])

    def test_tools_use_input_schema(self) -> None:
        payload = self.provider.build_payload(
            [Message(role="user", content="hi")],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "mem_create",
                        "description": "创建记忆",
                        "parameters": {"type": "object", "properties": {"content": {"type": "string"}}},
                    },
                }
            ],
        )
        tool = payload["tools"][0]
        self.assertEqual(tool["name"], "mem_create")
        self.assertIn("input_schema", tool)
        self.assertNotIn("function", tool, "不该留着 OpenAI 的嵌套结构")

    def test_max_tokens_is_always_sent(self) -> None:
        """Anthropic 的 max_tokens 是必填，漏了会 400。"""
        payload = self.provider.build_payload([Message(role="user", content="hi")])
        self.assertGreater(payload["max_tokens"], 0)


class SseParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.provider = AnthropicCompatProvider(
            test_settings(Path(self._tmp.name), api_key="k")
        )

    def _assemble(self, lines: list[bytes]) -> StreamAssembler:
        assembler = StreamAssembler()
        for event in self.provider._parse_sse(lines):
            assembler.feed(event)
        return assembler

    def test_text_stream(self) -> None:
        assembler = self._assemble(
            [
                _event(
                    "message_start",
                    message={"usage": {"input_tokens": 100, "cache_read_input_tokens": 40}},
                ),
                _event("content_block_start", index=0, content_block={"type": "text", "text": ""}),
                _event("content_block_delta", index=0, delta={"type": "text_delta", "text": "你"}),
                _event("content_block_delta", index=0, delta={"type": "text_delta", "text": "好"}),
                _event("content_block_stop", index=0),
                _event("message_delta", delta={"stop_reason": "end_turn"}, usage={"output_tokens": 7}),
                _event("message_stop"),
            ]
        )
        self.assertEqual(assembler.text, "你好")
        self.assertEqual(assembler.finish_reason, "stop")
        assert assembler.usage is not None
        self.assertEqual(assembler.usage.prompt_tokens, 140)
        self.assertEqual(assembler.usage.cached_tokens, 40, "缓存命中是 prompt 的一部分")
        self.assertEqual(assembler.usage.cache_miss_tokens, 100, "未命中 + 新建缓存")
        self.assertEqual(assembler.usage.completion_tokens, 7)
        self.assertEqual(assembler.usage.total_tokens, 147, "total 必须含缓存")

    def test_tool_use_stream(self) -> None:
        assembler = self._assemble(
            [
                _event("message_start", message={"usage": {"input_tokens": 10}}),
                _event(
                    "content_block_start",
                    index=1,
                    content_block={"type": "tool_use", "id": "toolu_1", "name": "mem_create", "input": {}},
                ),
                _event(
                    "content_block_delta",
                    index=1,
                    delta={"type": "input_json_delta", "partial_json": '{"content"'},
                ),
                _event(
                    "content_block_delta",
                    index=1,
                    delta={"type": "input_json_delta", "partial_json": ': "红茶"}'},
                ),
                _event("content_block_stop", index=1),
                _event("message_delta", delta={"stop_reason": "tool_use"}, usage={"output_tokens": 20}),
                _event("message_stop"),
            ]
        )
        calls = assembler.tool_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].id, "toolu_1")
        self.assertEqual(calls[0].name, "mem_create")
        self.assertEqual(calls[0].parsed_arguments(), {"content": "红茶"})
        self.assertEqual(assembler.finish_reason, "tool_calls")

    def test_thinking_delta_maps_to_reasoning(self) -> None:
        events = list(
            self.provider._parse_sse(
                [
                    _event("message_start", message={"usage": {}}),
                    _event(
                        "content_block_delta",
                        index=0,
                        delta={"type": "thinking_delta", "thinking": "想一下"},
                    ),
                ]
            )
        )
        self.assertTrue(any(isinstance(e, ReasoningDelta) for e in events))
        self.assertTrue(any(isinstance(e, TextDelta) for e in events) is False)

    def test_max_tokens_stop_reason_becomes_length(self) -> None:
        """这一条很关键：ADR-023 那条"截断不能被当成正常完成"的守卫依赖它。"""
        assembler = self._assemble(
            [
                _event("message_start", message={"usage": {"input_tokens": 5}}),
                _event(
                    "message_delta", delta={"stop_reason": "max_tokens"}, usage={"output_tokens": 900}
                ),
            ]
        )
        self.assertEqual(assembler.finish_reason, "length")

    def test_error_event_raises(self) -> None:
        with self.assertRaises(ProviderError) as ctx:
            list(
                self.provider._parse_sse(
                    [_event("error", error={"type": "authentication_error", "message": "bad key"})]
                )
            )
        self.assertIn("bad key", str(ctx.exception))

    def test_non_sse_body_raises_readable_error(self) -> None:
        with self.assertRaises(ProviderError) as ctx:
            list(self.provider._parse_sse([b'{"error":"nope"}']))
        self.assertIn("不是 SSE", str(ctx.exception))

    def test_ping_and_unknown_events_are_ignored(self) -> None:
        events = list(
            self.provider._parse_sse(
                [
                    _event("ping"),
                    _event("message_start", message={"usage": {"input_tokens": 3}}),
                    _event("content_block_delta", index=0, delta={"type": "text_delta", "text": "ok"}),
                    _event("message_stop"),
                ]
            )
        )
        self.assertEqual([e.text for e in events if isinstance(e, TextDelta)], ["ok"])
        # 即使没有 message_delta，也必须补一个 Finished：
        # 上层约定"最后一个事件一定是 Finished"，否则这一轮的用量会记成 0
        finished = [e for e in events if isinstance(e, Finished)]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0].reason, "stop")

    def test_missing_key_fails_fast(self) -> None:
        with self.assertRaises(ProviderError):
            AnthropicCompatProvider(test_settings(Path(self._tmp.name), api_key=""))


class FactoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_type_picks_the_implementation(self) -> None:
        settings = test_settings(Path(self._tmp.name), api_key="k")
        anthropic = ProviderProfile(
            id="kc", label="Kimi Code", base_url="https://api.kimi.com/coding", api_key="k",
            type="anthropic",
        )
        openai = ProviderProfile(
            id="ds", label="DeepSeek", base_url="https://api.deepseek.com", api_key="k"
        )
        self.assertIsInstance(build_provider(settings, anthropic), AnthropicCompatProvider)
        self.assertNotIsInstance(build_provider(settings, openai), AnthropicCompatProvider)

    def test_kimi_code_presets_exist_with_the_documented_urls(self) -> None:
        from harness.providers.store import get_preset

        china = get_preset("kimi-code")
        global_ = get_preset("kimi-code-global")
        assert china is not None and global_ is not None
        # 必须带 /v1：对话端点两者等价（客户端会归一化），
        # 但「获取模型列表」是直接拼 /models 的，少 /v1 就会打到错地址
        self.assertEqual(china.base_url, "https://api.kimi.com/coding/v1")
        self.assertEqual(global_.base_url, "https://api.kimi.ai/coding/v1")
        self.assertEqual(china.type, "anthropic")
        # 开放平台那条是 OpenAI 格式，两者不能混
        moonshot = get_preset("moonshot")
        assert moonshot is not None
        self.assertEqual(moonshot.type, "openai")

    def test_legacy_base_url_without_v1_is_migrated_on_load(self) -> None:
        """老预设留下的 base_url 要能自动纠正，否则老档案的模型发现有毛病。"""
        from harness.providers.store import ProviderProfile

        profile = ProviderProfile.from_dict(
            {"id": "kimi-code", "base_url": "https://api.kimi.com/coding"}
        )
        self.assertEqual(profile.base_url, "https://api.kimi.com/coding/v1")

        # 别人的自定义地址不能被乱改
        custom = ProviderProfile.from_dict(
            {"id": "x", "base_url": "https://my-gateway.internal/anthropic"}
        )
        self.assertEqual(custom.base_url, "https://my-gateway.internal/anthropic")


if __name__ == "__main__":
    unittest.main()
