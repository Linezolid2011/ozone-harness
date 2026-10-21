"""Provider 抽象。

上层（AgentLoop / ContextBuilder）只认这个接口，
所以将来接本地模型或换厂商时，改动被限制在 providers/ 目录内。
"""

from __future__ import annotations

from typing import Any, Iterator, Protocol, Sequence

from ..llm_types import Message, StreamEvent


class ProviderError(RuntimeError):
    """模型后端的统一异常。上层只捕获这一个类型。

    `status` 是 HTTP 状态码（拿不到就是 None：网络错误、SSE 里夹的错误事件等）。
    上层靠它区分"请求被拒"与"暂时故障"：契约 §13.5 只在
    `status ∈ {400, 404, 415, 422}` 时才把"图片没送出去"记进历史，
    `429`/`5xx`/网络错误一律不动历史（那些是暂时的，图还在，重试就好）。

    **可选**是刻意的：既有的一堆 `raise ProviderError("...")` 一个字都不用改，
    而"拿不到状态码"本身就是一个有意义的信息（= 不是明确的拒绝）。
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class LLMProvider(Protocol):
    name: str

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]] = (),
        *,
        max_tokens: int | None = None,
    ) -> Iterator[StreamEvent]:
        """以流式方式产出 delta 事件，最后一个事件应为 Finished。"""
        ...
