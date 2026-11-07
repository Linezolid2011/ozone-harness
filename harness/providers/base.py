"""Provider 抽象。

上层（AgentLoop / ContextBuilder）只认这个接口，
所以将来接本地模型或换厂商时，改动被限制在 providers/ 目录内。
"""

from __future__ import annotations

import urllib.parse
import urllib.request
from typing import Any, Iterator, Protocol, Sequence

from ..llm_types import Message, StreamEvent


_SECRET_HEADERS = ("Authorization", "X-api-key")


def _origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    default_port = {"http": 80, "https": 443}.get(scheme)
    port = parsed.port if parsed.port is not None else default_port
    return scheme, parsed.hostname, port


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """跨源跳转不复制供应商密钥；同源跳转保持现有请求行为。"""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        try:
            same_origin = _origin(req.full_url) == _origin(redirected.full_url)
        except ValueError:
            # 无法归一化端口时按跨源处理，不能让解析歧义变成密钥泄露。
            same_origin = False
        if not same_origin:
            for header in _SECRET_HEADERS:
                redirected.remove_header(header)
        return redirected


def open_provider_request(
    request: urllib.request.Request, *, timeout: float
) -> Any:
    """打开 provider 请求，并用共享重定向策略保护 Authorization/x-api-key。"""
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    return opener.open(request, timeout=timeout)


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


def record_prompt(
    provider: LLMProvider, messages: Sequence[Message]
) -> list[dict[str, Any]]:
    """生成可落盘的 prompt，同时允许 provider 应用真实请求的消息过滤。

    图片先走 `Message.to_prompt_record()`，provider 钩子只收到脱敏字典，不会触发
    base64 读取。没有钩子的 FakeProvider / 第三方 provider 保持原有通用消息形状。
    """
    records = [message.to_prompt_record() for message in messages]
    filter_records = getattr(provider, "filter_prompt_records", None)
    if filter_records is not None:
        return list(filter_records(records))
    return records
