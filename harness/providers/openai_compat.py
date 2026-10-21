"""OpenAI 兼容的流式客户端 —— 一个 provider 覆盖绝大多数供应商。

为什么只写一个：`/chat/completions` 这套格式事实上是行业通用标准，
DeepSeek、Kimi、智谱、通义、硅基流动、OpenAI、OpenRouter、本地 Ollama/vLLM
都用它。所以"支持多供应商"不等于"写很多个 provider"，而是
**把差异参数化**：base_url / api_key / model / 是否回传思维链 / 支持哪些 thinking 档位。

真正有差异的只有几处，都已经参数化：
  * `send_reasoning_content` —— DeepSeek **要求**回传历史思维链（漏传会 400，见 ADR-008）；
    而 OpenAI 官方对消息里的未知字段更严格，所以默认对其他供应商不回传。
  * `reasoning_effort` —— 只在该供应商声明了可选档位时才下发，
    避免给不认识的供应商塞一个它不认的参数。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Iterator, Sequence

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


def _usage_from_api(raw: dict[str, Any] | None) -> Usage | None:
    if not raw:
        return None
    details = raw.get("completion_tokens_details") or {}
    prompt_tokens = int(raw.get("prompt_tokens") or 0)
    completion_tokens = int(raw.get("completion_tokens") or 0)
    total = int(raw.get("total_tokens") or 0) or (prompt_tokens + completion_tokens)
    return Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total,
        reasoning_tokens=int(details.get("reasoning_tokens") or 0),
        cached_tokens=int(raw.get("prompt_cache_hit_tokens") or 0),
        cache_miss_tokens=int(raw.get("prompt_cache_miss_tokens") or 0),
    )


class OpenAICompatProvider:
    """一个供应商 + 一个模型。多供应商靠"多家配置 + 每次请求选一家"来实现。"""

    def __init__(
        self,
        settings: Settings,
        *,
        label: str = "",
        send_reasoning_content: bool = False,
    ) -> None:
        # `label` 是**显示名**，会出现在"{label} 返回 HTTP …"这类报错里。
        # 空串 = 用当前语言的默认名（**现取**：语言是运行期可切的，
        # 默认参数在 import 时就算好了，所以不能用 `label="OpenAI 兼容"`）。
        label = label or t("provider.label.openai")
        if not settings.api_key:
            raise ProviderError(t("provider.openai.no_key", label=label))
        self._settings = settings
        self.label = label
        self.name = label
        self._send_reasoning = send_reasoning_content

    # ------------------------------------------------------------------
    # 请求构造
    # ------------------------------------------------------------------

    def _message_payload(self, message: Message) -> dict[str, Any]:
        # 带图 user 消息的 content 块数组（text + image_url data URL）由
        # `Message.to_api()` 产出 —— 这里刻意**不再加工**：图片块的形状只有一处
        # 定义，才不会有"某个 provider 忘了带图"这种静默丢图。
        payload = message.to_api()
        if not self._send_reasoning:
            payload.pop("reasoning_content", None)
        return payload

    def build_payload(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]] = (),
        *,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._settings.model,
            "messages": [self._message_payload(message) for message in messages],
            "stream": True,
            "max_tokens": max_tokens or self._settings.max_output_tokens,
        }
        if tools:
            payload["tools"] = list(tools)
        if self._settings.reasoning_effort:
            payload["reasoning_effort"] = self._settings.reasoning_effort
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
            "Authorization": f"Bearer {self._settings.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            emitted = False
            try:
                request = urllib.request.Request(
                    self._settings.api_url, data=body, headers=headers, method="POST"
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
                # 消息文案一个字都不改（测试与用户都在看它），只额外带上状态码：
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

        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                if len(raw_preview) < 5:
                    raw_preview.append(line)
                continue

            saw_payload = True
            data = line[5:].strip()
            if data == "[DONE]":
                yield Finished(reason=None, usage=None)
                return

            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, dict):
                # 有些代理/网关会吐 data: 0 或 data: []：合法 JSON、但不是对象。
                # 不管它的话，下面一句 chunk.get(...) 就抛 AttributeError，
                # 而那既不是 ProviderError 也不是网络错误 —— 整轮直接崩掉。
                continue

            if isinstance(chunk.get("error"), dict):
                message = chunk["error"].get("message") or chunk["error"]
                raise ProviderError(
                    t("provider.http.error", label=self.label, message=message)
                )

            usage = _usage_from_api(chunk.get("usage"))
            choices = chunk.get("choices") or []

            if not choices:
                if usage is not None:
                    yield Finished(reason=None, usage=usage)
                continue

            choice = choices[0]
            delta = choice.get("delta") or {}

            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning:
                yield ReasoningDelta(text=reasoning)

            text = delta.get("content")
            if text:
                yield TextDelta(text=text)

            for call in delta.get("tool_calls") or []:
                function = call.get("function") or {}
                yield ToolCallDelta(
                    index=int(call.get("index") or 0),
                    id=call.get("id"),
                    name=function.get("name"),
                    arguments_fragment=function.get("arguments"),
                )

            finish_reason = choice.get("finish_reason")
            if finish_reason or usage is not None:
                yield Finished(reason=finish_reason, usage=usage)

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
            if parsed.get("message"):
                return str(parsed["message"])
        except json.JSONDecodeError:
            pass
        return body[:400]
