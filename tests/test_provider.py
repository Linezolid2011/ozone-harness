"""DeepSeek 后端测试：SSE 解析与请求构造。用真实抓到的报文形状，但不联网。"""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock

from harness import i18n
from harness.llm_types import (
    Finished,
    ImagePart,
    Message,
    ReasoningDelta,
    StreamAssembler,
    TextDelta,
    ToolCallDelta,
    ToolCall,
)
from harness.providers.base import ProviderError, _SafeRedirectHandler, record_prompt
from harness.providers.openai_compat import OpenAICompatProvider

from .helpers import Harness, test_settings, text_reply, tool_reply


def deepseek_style(settings):
    """DeepSeek 的语义 = "预设里把『回传历史思维链』打开的那一家"。

    去特殊化之后没有专门的 DeepSeek 类了 —— 差异只是档案上的一个开关，
    所以这里按它真实的样子构造（老板本的 DeepSeekProvider 就是这么个薄子类）。
    """
    return OpenAICompatProvider(settings, label="DeepSeek", send_reasoning_content=True)


class OfflineOpenAI(OpenAICompatProvider):
    """使用真实 payload 构造器，但用剧本代替网络。"""

    def __init__(self, settings, script, *, send_reasoning: bool) -> None:
        super().__init__(
            settings, label="离线 OpenAI", send_reasoning_content=send_reasoning
        )
        self.script = [list(item) for item in script]
        self.payloads: list[dict[str, Any]] = []

    def stream(self, messages, tools=(), *, max_tokens=None):
        self.payloads.append(self.build_payload(messages, tools, max_tokens=max_tokens))
        yield from self.script.pop(0)


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


class RedirectTests(unittest.TestCase):
    def _redirect(self, target: str) -> tuple[urllib.request.Request, urllib.request.Request]:
        request = urllib.request.Request(
            "https://api.example.com/v1/chat",
            data=b"{}",
            headers={"Authorization": "Bearer secret", "x-api-key": "secret"},
            method="POST",
        )
        redirected = _SafeRedirectHandler().redirect_request(
            request, None, 302, "Found", {}, target
        )
        assert redirected is not None
        return request, redirected

    def test_initial_and_same_origin_requests_keep_credentials(self) -> None:
        request, redirected = self._redirect("https://api.example.com:443/next")
        for candidate in (request, redirected):
            self.assertEqual(candidate.get_header("Authorization"), "Bearer secret")
            self.assertEqual(candidate.get_header("X-api-key"), "secret")

    def test_cross_origin_redirects_strip_credentials(self) -> None:
        targets = [
            "https://other.example.com/next",
            "http://api.example.com/next",
            "https://api.example.com:444/next",
        ]
        for target in targets:
            with self.subTest(target=target):
                _request, redirected = self._redirect(target)
                self.assertIsNone(redirected.get_header("Authorization"))
                self.assertIsNone(redirected.get_header("X-api-key"))

    def test_openai_stream_uses_the_safe_open_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = deepseek_style(test_settings(Path(tmp)))
            response = mock.MagicMock()
            response.__iter__.return_value = iter([_chunk({}, finish="stop")])
            with mock.patch(
                "harness.providers.openai_compat.open_provider_request"
            ) as safe_open:
                safe_open.return_value.__enter__.return_value = response
                events = list(provider.stream([Message(role="user", content="hi")]))
            self.assertTrue(any(isinstance(event, Finished) for event in events))
            request = safe_open.call_args.args[0]
            self.assertEqual(request.get_header("Authorization"), "Bearer test-key")


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
        lines = [
            b"data: {oops\n",
            _chunk({"content": "ok"}),
            _chunk({}, finish="stop"),
            b"data: [DONE]\n",
        ]
        events = list(self.provider._parse_sse(lines))
        texts = [event.text for event in events if isinstance(event, TextDelta)]
        self.assertEqual(texts, ["ok"])

    def test_usage_only_tail_chunk(self) -> None:
        tail = (
            "data: "
            + json.dumps({"choices": [], "usage": _REAL_USAGE})
            + "\n"
        ).encode("utf-8")
        lines = [_chunk({"content": "done"}), tail]
        events = list(self.provider._parse_sse(lines))
        finished = [event for event in events if isinstance(event, Finished)]
        self.assertEqual(len(finished), 1)
        self.assertIsNone(finished[0].reason)
        self.assertIsNotNone(finished[0].usage)

    def test_eof_or_done_without_finished_is_truncated(self) -> None:
        for suffix in ([], [b"data: [DONE]\n"]):
            with self.subTest(suffix=suffix):
                lines = [_chunk({"content": "partial"}), *suffix]
                with i18n.use("en"):
                    with self.assertRaises(ProviderError) as ctx:
                        list(self.provider._parse_sse(lines))
                self.assertIn("terminal", str(ctx.exception))

    def test_cached_tokens_derive_miss_only_when_explicit_miss_is_absent(self) -> None:
        for explicit_miss, expected_miss in ((None, 25), (0, 0), (7, 7)):
            with self.subTest(explicit_miss=explicit_miss):
                usage = {
                    "prompt_tokens": 37,
                    "completion_tokens": 47,
                    "prompt_tokens_details": {"cached_tokens": 12},
                }
                if explicit_miss is not None:
                    usage["prompt_cache_miss_tokens"] = explicit_miss
                events = list(self.provider._parse_sse([_chunk({}, usage=usage)]))
                finished = next(event for event in events if isinstance(event, Finished))
                assert finished.usage is not None
                self.assertEqual(finished.usage.cached_tokens, 12)
                self.assertEqual(finished.usage.cache_miss_tokens, expected_miss)


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

    def test_include_usage_is_an_opt_in_stream_option(self) -> None:
        settings = test_settings(Path(self._tmp.name))
        without = OpenAICompatProvider(settings)
        payload = without.build_payload([Message(role="user", content="hi")])
        self.assertNotIn("stream_options", payload)

        with_usage = OpenAICompatProvider(settings, include_usage=True)
        payload = with_usage.build_payload([Message(role="user", content="hi")])
        self.assertEqual(payload["stream_options"], {"include_usage": True})

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

    def test_prompt_log_matches_the_actual_messages_in_both_reasoning_modes(self) -> None:
        for send_reasoning in (False, True):
            with self.subTest(send_reasoning=send_reasoning):
                root = Path(self._tmp.name) / str(send_reasoning)
                settings = test_settings(root)
                provider = OfflineOpenAI(
                    settings,
                    [
                        tool_reply(
                            [("c1", "mem_create", '{"content":"x"}')],
                            reasoning="历史思维链",
                        ),
                        text_reply("好"),
                    ],
                    send_reasoning=send_reasoning,
                )
                harness = Harness(settings, provider)  # type: ignore[arg-type]
                try:
                    harness.loop.run("记住")
                finally:
                    harness.close()

                logged = harness.log.prompts()[1]["messages"]
                actual = provider.payloads[1]["messages"]
                self.assertEqual(logged, actual)
                assistant = next(item for item in actual if item["role"] == "assistant")
                self.assertEqual(
                    "reasoning_content" in assistant, send_reasoning
                )

    def test_prompt_provider_filter_receives_only_redacted_messages(self) -> None:
        settings = test_settings(Path(self._tmp.name))
        provider = OpenAICompatProvider(settings, label="离线", send_reasoning_content=False)
        ghost = ImagePart(
            path=Path(self._tmp.name) / "不存在.png",
            mime="image/png",
            name="ghost.png",
            width=10,
            height=20,
            id="ghost",
        )

        records = record_prompt(
            provider,
            [
                Message(role="user", content="看图", images=[ghost]),
                Message(role="assistant", content="答", reasoning="不应记录成已发送"),
            ],
        )

        blob = json.dumps(records, ensure_ascii=False)
        self.assertNotIn("base64", blob)
        self.assertNotIn("image_url", blob)
        self.assertNotIn("reasoning_content", blob)
        self.assertEqual(records[0]["attachments"][0]["id"], "ghost")

    def test_missing_api_key_fails_fast(self) -> None:
        with self.assertRaises(ProviderError):
            deepseek_style(test_settings(Path(self._tmp.name), api_key=""))


if __name__ == "__main__":
    unittest.main()
