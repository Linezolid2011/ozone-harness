"""DeepSeek 后端测试：SSE 解析与请求构造。用真实抓到的报文形状，但不联网。"""

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
    ToolCallDelta,
    ToolCall,
)
from harness.providers.base import ProviderError
from harness.providers.openai_compat import OpenAICompatProvider

from .helpers import test_settings


def deepseek_style(settings):
    """DeepSeek 的语义 = "预设里把『回传历史思维链』打开的那一家"。

    去特殊化之后没有专门的 DeepSeek 类了 —— 差异只是档案上的一个开关，
    所以这里按它真实的样子构造（老板本的 DeepSeekProvider 就是这么个薄子类）。
    """
    return OpenAICompatProvider(settings, label="DeepSeek", send_reasoning_content=True)


def _chunk(
    delta: dict[str, Any],
    finish: str | None = None,
    usage: dict[str, Any] | None = None,
) -> bytes:
    payload: dict[str, Any] = {
        "id": "test",
        "object": "chat.completion.chunk",
        "model": "deepseek-flash",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage is not None:
        payload["usage"] = usage
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


_REAL_USAGE = {
    "prompt_tokens": 37,
    "completion_tokens": 47,
    "total_tokens": 84,
    "prompt_tokens_details": {"cached_tokens": 12},
    "completion_tokens_details": {"reasoning_tokens": 38},
    "prompt_cache_hit_tokens": 12,
    "prompt_cache_miss_tokens": 25,
}


class SseParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.provider = deepseek_style(test_settings(Path(self._tmp.name)))

    def test_parses_a_realistic_stream(self) -> None:
        lines = [
            _chunk({"role": "assistant", "content": None, "reasoning_content": ""}),
            _chunk({"content": None, "reasoning_content": "We"}),
            _chunk({"content": None, "reasoning_content": " need"}),
            _chunk({"content": "PONG", "reasoning_content": None}),
            _chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "mem_create", "arguments": '{"content"'},
                        }
                    ]
                }
            ),
            _chunk({"tool_calls": [{"index": 0, "function": {"arguments": ': "hi"}'}}]}),
            _chunk({"content": "", "reasoning_content": None}, finish="tool_calls", usage=_REAL_USAGE),
            b"data: [DONE]\n",
        ]

        assembler = StreamAssembler()
        for event in self.provider._parse_sse(lines):
            assembler.feed(event)

        self.assertEqual(assembler.reasoning, "We need")
        self.assertEqual(assembler.text, "PONG")
        self.assertEqual(assembler.finish_reason, "tool_calls")

        calls = assembler.tool_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].id, "call_1")
        self.assertEqual(calls[0].name, "mem_create")
        self.assertEqual(calls[0].arguments, '{"content": "hi"}')
        self.assertEqual(calls[0].parsed_arguments(), {"content": "hi"})

        assert assembler.usage is not None
        self.assertEqual(assembler.usage.prompt_tokens, 37)
        self.assertEqual(assembler.usage.completion_tokens, 47)
        self.assertEqual(assembler.usage.total_tokens, 84)
        self.assertEqual(assembler.usage.reasoning_tokens, 38)
        self.assertEqual(assembler.usage.cached_tokens, 12)
        self.assertEqual(assembler.usage.cache_miss_tokens, 25)

    def test_tolerates_comment_and_blank_lines(self) -> None:
        lines = [
            b": keep-alive\n",
            b"\n",
            _chunk({"content": "hi"}),
            _chunk({}, finish="stop", usage=_REAL_USAGE),
        ]
        events = list(self.provider._parse_sse(lines))
        self.assertTrue(any(isinstance(event, TextDelta) for event in events))
        self.assertTrue(any(isinstance(event, Finished) for event in events))

    def test_non_sse_body_raises_readable_error(self) -> None:
        lines = [b'{"error":{"message":"Invalid API key"}}']
        with self.assertRaises(ProviderError) as ctx:
            list(self.provider._parse_sse(lines))
        self.assertIn("不是 SSE", str(ctx.exception))

    def test_malformed_json_lines_are_skipped(self) -> None:
        lines = [b"data: {oops\n", _chunk({"content": "ok"}), b"data: [DONE]\n"]
        events = list(self.provider._parse_sse(lines))
        texts = [event.text for event in events if isinstance(event, TextDelta)]
        self.assertEqual(texts, ["ok"])

    def test_usage_only_tail_chunk(self) -> None:
        lines = [_chunk({"content": "done"}), _chunk({}, usage=_REAL_USAGE)]
        events = list(self.provider._parse_sse(lines))
        finished = [event for event in events if isinstance(event, Finished)]
        self.assertTrue(any(event.usage is not None for event in finished))


class PayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_reasoning_effort_only_sent_when_configured(self) -> None:
        without = deepseek_style(test_settings(Path(self._tmp.name)))
        payload = without.build_payload([Message(role="user", content="hi")])
        self.assertNotIn("reasoning_effort", payload)

        with_effort = deepseek_style(
            test_settings(Path(self._tmp.name), reasoning_effort="low")
        )
        payload = with_effort.build_payload([Message(role="user", content="hi")])
        self.assertEqual(payload["reasoning_effort"], "low")

    def test_payload_shape(self) -> None:
        provider = deepseek_style(test_settings(Path(self._tmp.name)))
        tools = [
            {
                "type": "function",
                "function": {"name": "mem_create", "description": "d", "parameters": {}},
            }
        ]
        history = [
            Message(role="assistant", content="", tool_calls=[ToolCall("c1", "mem_create", "{}")], reasoning="内部思维"),
            Message(role="tool", tool_call_id="c1", name="mem_create", content='{"ok":true}'),
        ]
        payload = provider.build_payload(history, tools)

        self.assertTrue(payload["stream"])
        self.assertEqual(payload["model"], "fake-model")
        self.assertEqual(payload["tools"], tools)
        self.assertEqual(payload["messages"][0]["reasoning_content"], "内部思维")
        self.assertEqual(payload["messages"][0]["tool_calls"][0]["id"], "c1")
        self.assertEqual(payload["messages"][1]["tool_call_id"], "c1")

    def test_missing_api_key_fails_fast(self) -> None:
        with self.assertRaises(ProviderError):
            deepseek_style(test_settings(Path(self._tmp.name), api_key=""))


if __name__ == "__main__":
    unittest.main()
