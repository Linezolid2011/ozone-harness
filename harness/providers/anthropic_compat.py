"""Anthropic Messages API 兼容客户端。

为什么需要它（而不是只留 OpenAI 兼容那一个）：**Kimi Code（订阅制 token plan）
只提供 Anthropic 兼容端点** —— `https://api.kimi.com/coding/`（国内）与
`https://api.kimi.ai/coding/`（海外）。Kimi 开放平台（按量付费）才是 OpenAI 格式的
`https://api.moonshot.cn/v1`。来源见 Kimi Code FAQ 与 kimi-cli 的 providers 文档。

两套协议的差异不只是 URL，消息结构本身就不同，所以必须单独实现：

| | OpenAI 格式 | Anthropic 格式 |
|---|---|---|
| system | 放在 messages 里 | 顶层 `system` 参数 |
| 工具定义 | `{type:"function", function:{name, description, parameters}}` | `{name, description, input_schema}` |
| 工具调用 | assistant 的 `tool_calls` 字段 | assistant 的 `tool_use` 内容块 |
| 工具结果 | `role:"tool"` 消息 | `role:"user"` + `tool_result` 内容块，且必须紧跟调用 |
| 流式事件 | `choices[].delta` | `content_block_start/delta` 事件 |
| 停止原因 | `stop` / `tool_calls` / `length` | `end_turn` / `tool_use` / `max_tokens` |

**已实测通过**：2026-10-06 用真实 Kimi Code key 跑通了一整轮对话
（`provider=kimi-code`、`model=kimi-for-coding`、steps=1、reason=completed、
prompt 1071 / completion 57），模型正确回答了问题、并按记忆主动提起用户偏好。
—— 这一条留在文件头，是因为"协议实现没验证过"和"验证过了"必须能一眼区分。
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from typing import Any, Iterator, Sequence

from ..attachments import EXT_MIME, read_bytes
from ..config import Settings
from ..i18n import t
from ..llm_types import (
    Finished,
    Message,
    ReasoningDelta,
    StreamEvent,
    TextDelta,
    ToolCallDelta,
    Usage,
)
from .base import ProviderError

_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
ANTHROPIC_VERSION = "2023-06-01"

# Anthropic 的停止原因 → 我们内部的词汇。
# max_tokens → length 很关键：ADR-023 那条"截断不能被当成正常完成"的守卫依赖它。
_STOP_REASON = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "refusal": "stop",
}


def _messages_url(base_url: str) -> str:
    """把各种写法归一成 `<base>/v1/messages`。

    用户可能填 `https://api.anthropic.com`、`https://api.kimi.com/coding`，
    也可能照 OpenAI 的习惯填成 `.../v1`。三种都要能用。
    """
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base + "/v1/messages"


class AnthropicCompatProvider:
    name = "anthropic"

    def __init__(
        self,
        settings: Settings,
        *,
        label: str = "",
        send_reasoning_content: bool = False,
    ) -> None:
        # 同 `OpenAICompatProvider`：默认显示名**现取**（空串 = 当前语言的默认名）。
        label = label or t("provider.label.anthropic")
        if not settings.api_key:
            raise ProviderError(t("provider.anthropic.no_key"))
        self._settings = settings
        self.label = label
        self.name = label
        # Anthropic 的思维块需要带签名的 thinking block 才能回传，实现成本高。
        # 这里默认不回传，也不影响对话（只是模型看不到自己上一步的思考）。
        self._send_reasoning = send_reasoning_content

    # ------------------------------------------------------------------
    # 消息转换
    # ------------------------------------------------------------------

    def _convert(self, messages: Sequence[Message]) -> tuple[str, list[dict[str, Any]]]:
        """折成 Anthropic 的形状：(system 文本, messages 数组)。"""
        system_parts: list[str] = []
        converted: list[dict[str, Any]] = []

        for message in messages:
            if message.role == "system":
                if message.content:
                    system_parts.append(message.content)
                continue

            if message.role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id or "",
                    "content": message.content or "",
                }
                # Anthropic 要求 tool_result 必须紧跟在对应的 tool_use 之后，
                # 所以我们把连续的多个结果合并进同一条 user 消息。
                if converted and converted[-1]["role"] == "user" and isinstance(
                    converted[-1]["content"], list
                ) and all(
                    isinstance(item, dict) and item.get("type") == "tool_result"
                    for item in converted[-1]["content"]
                ):
                    converted[-1]["content"].append(block)
                else:
                    converted.append({"role": "user", "content": [block]})
                continue

            if message.role == "assistant":
                blocks: list[dict[str, Any]] = []
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                for call in message.tool_calls:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call.id or "tool_use",
                            "name": call.name,
                            "input": call.parsed_arguments(),
                        }
                    )
                if not blocks:
                    continue
                converted.append({"role": "assistant", "content": blocks})
                continue

            # user
            if message.images:
                # **带图消息绝不能走下面那条字符串路径**：旧实现把 user 的 content
                # 直接当字符串用，图片到了这里会被静默拼掉（或者更糟 —— 被人"顺手"
                # 拼成一段带路径的文本发给模型）。图片只能以 image 块出现。
                blocks = self._user_blocks(message)
                if (
                    converted
                    and converted[-1]["role"] == "user"
                    and isinstance(converted[-1]["content"], str)
                ):
                    # 与上一条纯文本 user 合并（保持既有行为：核心记忆块 + 真实提问
                    # 不该被拆成两条消息）。上一条是块数组时不合并 —— 那通常是
                    # tool_result，它必须紧跟自己的 tool_use，往里塞别的内容只会
                    # 让协议校验变得更难预测。
                    previous = converted[-1]["content"]
                    merged: list[dict[str, Any]] = (
                        [{"type": "text", "text": previous}] if previous else []
                    )
                    merged.extend(blocks)
                    converted[-1]["content"] = merged
                else:
                    converted.append({"role": "user", "content": blocks})
                continue
            if not message.content:
                continue
            if converted and converted[-1]["role"] == "user" and isinstance(
                converted[-1]["content"], str
            ):
                # 连续两条 user（例如核心记忆块 + 真实提问）合并成一条
                converted[-1]["content"] = (
                    converted[-1]["content"] + "\n\n" + message.content
                )
            else:
                converted.append({"role": "user", "content": message.content})

        return "\n\n".join(part for part in system_parts if part), converted

    @staticmethod
    def _user_blocks(message: Message) -> list[dict[str, Any]]:
        """把一条**带图**的 user 消息折成 Anthropic 的内容块（契约 §4）。

        文本块排在最前、图片块按用户添加顺序跟在后面；只发图不说话时**不发空的
        文本块**（实测空文本块也被接受，但"没有文本就别发"更干净）。
        `media_type` 用落盘记录里的 mime；万一它缺了，按扩展名兜底 ——
        宁可给一个正确的类型，也不要给一个空的必填字段。
        """
        blocks: list[dict[str, Any]] = []
        if message.content:
            blocks.append({"type": "text", "text": message.content})
        for image in message.images:
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image.mime
                        or EXT_MIME.get(image.ext, "application/octet-stream"),
                        "data": base64.b64encode(read_bytes(image.path)).decode("ascii"),
                    },
                }
            )
        return blocks

    @staticmethod
    def _convert_tools(tools: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for spec in tools:
            function = spec.get("function") or {}
            out.append(
                {
                    "name": function.get("name"),
                    "description": function.get("description") or "",
                    "input_schema": function.get("parameters")
                    or {"type": "object", "properties": {}},
                }
            )
        return out

    def build_payload(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]] = (),
        *,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        system, converted = self._convert(messages)
        payload: dict[str, Any] = {
            "model": self._settings.model,
            "max_tokens": max_tokens or self._settings.max_output_tokens,
            "messages": converted,
            "stream": True,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = self._convert_tools(tools)
        return payload

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]] = (),
        *,
        max_tokens: int | None = None,
    ) -> Iterator[StreamEvent]:
        payload = self.build_payload(messages, tools, max_tokens=max_tokens)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "x-api-key": self._settings.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        url = _messages_url(self._settings.base_url)

        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            emitted = False
            try:
                request = urllib.request.Request(
                    url, data=body, headers=headers, method="POST"
                )
            except ValueError as exc:  # 地址本身不合法（缺 scheme 等），urlopen 之前就炸
                raise ProviderError(
                    t("provider.store.base_url_scheme")
                ) from exc
            try:
                with urllib.request.urlopen(
                    request, timeout=self._settings.request_timeout
                ) as response:
                    for event in self._parse_sse(response):
                        emitted = True
                        yield event
                return
            except urllib.error.HTTPError as exc:
                detail = self._read_error_body(exc)
                # 文案一个字都不改（测试与用户都在看它），只额外带上状态码：
                # 上层要判断"这是不是一次明确的拒绝"（契约 §13.5）。
                last_error = ProviderError(
                    t("provider.http.status", label=self.label, status=exc.code, detail=detail),
                    status=exc.code,
                )
                if emitted or exc.code not in _RETRYABLE_STATUS or attempt == _MAX_ATTEMPTS:
                    raise last_error from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = ProviderError(
                    t("provider.http.network", label=self.label, detail=exc)
                )
                if emitted or attempt == _MAX_ATTEMPTS:
                    raise last_error from exc
            time.sleep(1.5 * attempt)
        if last_error:
            raise last_error
        raise ProviderError(t("provider.http.unknown_failure"))

    # ------------------------------------------------------------------
    # SSE 解析
    # ------------------------------------------------------------------

    def _parse_sse(self, response: Any) -> Iterator[StreamEvent]:
        saw_payload = False
        raw_preview: list[str] = []
        prompt_tokens = 0
        cached_tokens = 0
        cache_miss_tokens = 0
        finished = False

        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                if len(raw_preview) < 5:
                    raw_preview.append(line)
                continue

            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, dict):
                # 合法 JSON 但不是对象（data: [] 之类）：跳过，别让 .get() 抛
                # AttributeError 打断整轮（见 openai_compat 的同一条注释）。
                continue

            saw_payload = True
            kind = chunk.get("type")

            if kind == "error":
                error = chunk.get("error") or {}
                raise ProviderError(
                    t(
                        "provider.http.error",
                        label=self.label,
                        message=error.get("message") or error,
                    )
                )

            if kind == "message_start":
                usage = ((chunk.get("message") or {}).get("usage")) or {}
                prompt_tokens = int(usage.get("input_tokens") or 0)
                cached_tokens = int(usage.get("cache_read_input_tokens") or 0)
                cache_miss_tokens = prompt_tokens + int(
                    usage.get("cache_creation_input_tokens") or 0
                )
                continue

            if kind == "content_block_start":
                block = chunk.get("content_block") or {}
                if block.get("type") == "tool_use":
                    yield ToolCallDelta(
                        index=int(chunk.get("index") or 0),
                        id=block.get("id"),
                        name=block.get("name"),
                        # input 若已给全（非流式分片），直接当作参数碎片
                        arguments_fragment=(
                            json.dumps(block["input"], ensure_ascii=False)
                            if block.get("input")
                            else None
                        ),
                    )
                continue

            if kind == "content_block_delta":
                delta = chunk.get("delta") or {}
                delta_type = delta.get("type")
                if delta_type == "text_delta" and delta.get("text"):
                    yield TextDelta(text=delta["text"])
                elif delta_type == "thinking_delta" and delta.get("thinking"):
                    yield ReasoningDelta(text=delta["thinking"])
                elif delta_type == "input_json_delta" and delta.get("partial_json"):
                    yield ToolCallDelta(
                        index=int(chunk.get("index") or 0),
                        arguments_fragment=delta["partial_json"],
                    )
                continue

            if kind == "message_delta":
                delta = chunk.get("delta") or {}
                usage = chunk.get("usage") or {}
                reason = _STOP_REASON.get(str(delta.get("stop_reason") or ""), "stop")
                output_tokens = int(usage.get("output_tokens") or 0)
                finished = True
                yield Finished(
                    reason=reason,
                    usage=Usage(
                        prompt_tokens=prompt_tokens,
                        completion_tokens=output_tokens,
                        total_tokens=prompt_tokens + output_tokens,
                        reasoning_tokens=0,
                        cached_tokens=cached_tokens,
                        cache_miss_tokens=cache_miss_tokens,
                    ),
                )
                continue

            if kind == "message_stop":
                # 正常情况下 message_delta 先到；万一没到也要补一个 Finished ——
                # 上层约定"最后一个事件一定是 Finished"，否则这一轮的用量会记成 0。
                if not finished:
                    yield Finished(reason="stop", usage=None)
                return

        if not saw_payload:
            preview = " | ".join(raw_preview)[:400]
            raise ProviderError(
                t(
                    "provider.http.not_sse",
                    label=self.label,
                    preview=preview or t("provider.http.empty_body"),
                )
            )

    @staticmethod
    def _read_error_body(exc: urllib.error.HTTPError) -> str:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 读取失败不能掩盖原始错误
            return t("provider.http.error_unreadable")
        try:
            parsed = json.loads(body)
            error = parsed.get("error")
            if isinstance(error, dict):
                return str(error.get("message") or error)
            if error:
                return str(error)
        except json.JSONDecodeError:
            pass
        return body[:400]
