"""核心数据类型：消息、工具调用、流式事件、用量。

分层原则：这是 harness 内部的"通用语言"，不含任何 DeepSeek 特有字段命名。
所有厂商格式转换只发生在 providers/ 里，上层代码不认识具体模型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence, Union

# --------------------------------------------------------------------------
# 工具调用与消息
# --------------------------------------------------------------------------


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str = ""  # 保持厂商返回的原始 JSON 字符串（流式下是拼出来的）

    def arguments_are_valid_json(self) -> bool:
        text = (self.arguments or "").strip()
        if not text:
            return True
        try:
            return isinstance(json.loads(text), dict)
        except json.JSONDecodeError:
            return False

    def parsed_arguments(self) -> dict[str, Any]:
        """容错解析：模型给的 JSON 不合法时返回空 dict，由调用方决定怎么报错。"""
        text = (self.arguments or "").strip()
        if not text:
            return {}
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {"value": value}

    def to_api(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments or "{}"},
        }

    def to_record(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}

    @classmethod
    def from_record(cls, rec: dict[str, Any]) -> "ToolCall":
        return cls(
            id=str(rec.get("id") or ""),
            name=str(rec.get("name") or ""),
            arguments=str(rec.get("arguments") or ""),
        )


@dataclass
class ImagePart:
    """一条用户消息里的一张图（**已经落盘**的那一版）。

    这里存的是本地路径而不是字节：图片每步都要重发（历史里带图），
    把字节挂在消息对象上会让"同一张图"在内存里存在 N 份；路径 + 读取层的小 LRU
    才是对的形状（见 attachments.read_bytes）。

    `width/height` 是**落盘版**的尺寸，也是发给模型的那一版 —— 事件记录里
    另有一对 `bytes/width/height` 记用户上传的原始尺寸，那是给人看的。
    """

    path: Path
    mime: str = ""
    name: str = ""
    width: int = 0
    height: int = 0
    id: str = ""

    def __post_init__(self) -> None:
        # 允许调用方给字符串路径（测试和手写的调试代码会这么干）：
        # 后面 ext/data_url 都按 Path 处理，这里统一一次，免得在很远的 .suffix 上报错。
        if not isinstance(self.path, Path):
            self.path = Path(self.path)

    @property
    def ext(self) -> str:
        """扩展名。优先取路径后缀（落盘名就是哈希 + 真实扩展名），退回 MIME。"""
        suffix = str(self.path.suffix or "").lstrip(".").lower()
        if suffix:
            return suffix
        for known, mime in (("png", "image/png"), ("jpg", "image/jpeg"),
                            ("webp", "image/webp"), ("gif", "image/gif")):
            if self.mime == mime:
                return known
        return "png"

    def data_url(self) -> str:
        """渲染成 data URL。**只在发给模型的那条路上调用**，绝不进事件日志。"""
        # 延迟导入：attachments 顶层要用本模块的 ImagePart，顶层互相 import 会成环。
        from .attachments import data_url as _data_url

        return _data_url(self.path, self.ext)


@dataclass
class Message:
    role: str  # system | user | assistant | tool
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    # 图片附件。**只有 user 消息会有** —— 官方规定 system/assistant 带图直接 400，
    # 所以这里不做通用支持，只挂在用户消息上（历史重放时同理）。
    # 存的是"已经落盘的那一版"（缩放后的），path 指向 data/attachments 下的真实文件。
    images: list["ImagePart"] = field(default_factory=list)
    # 模型的思维链。**必须回传**：官方文档规定，携带 tools 的请求在后续所有请求中
    # 都要完整回传 reasoning_content，否则 API 返回 400；同时它会被拼进上下文，
    # 让模型在工具循环的多步之间保持思考连续性。
    # 参考：https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/
    reasoning: str | None = None
    # 重放时的来源事件序号。只用于把请求级失败追回原消息，不进入 API 或事件正文。
    source_seq: int | None = None

    def to_api(self) -> dict[str, Any]:
        if self.role == "tool":
            payload: dict[str, Any] = {"role": "tool", "content": self.content}
            if self.tool_call_id:
                payload["tool_call_id"] = self.tool_call_id
            # name 在 OpenAI 兼容协议里是可选字段，但下发它兼容性更好、排查也更清楚
            if self.name:
                payload["name"] = self.name
            return payload
        if self.role == "user" and self.images:
            # 带图消息的 content 变成块数组（契约 §4）：
            # 文本块**排在最前**，且**文本为空时不发空文本块**（用户只发图不说话）；
            # 图片块按用户添加顺序跟在后面。
            blocks: list[dict[str, Any]] = []
            if self.content:
                blocks.append({"type": "text", "text": self.content})
            for image in self.images:
                blocks.append(
                    {"type": "image_url", "image_url": {"url": image.data_url()}}
                )
            return {"role": "user", "content": blocks}
        payload = {"role": self.role, "content": self.content or ""}
        if self.tool_calls:
            payload["tool_calls"] = [call.to_api() for call in self.tool_calls]
        if self.role == "assistant" and self.reasoning:
            payload["reasoning_content"] = self.reasoning
        return payload

    def to_prompt_record(self) -> dict[str, Any]:
        """`prompt_built` 事件专用的脱敏形态（契约 §5，硬规则）。

        **绝不能在这条路径上碰 base64**：prompt_built 每一步都写一次，
        一张 8 MiB 的图 base64 之后往 JSONL 里灌，日志立刻既不能 grep 也不能重放。
        所以带图消息在这里被渲染成"形状记录"（id/name/mime/宽高），图片本身不进日志。

        注意实现上是**先判断再走 to_api**：不是"调 to_api 然后删掉 base64" ——
        那样已经白白读过一遍磁盘、做过一遍 base64 编码了。
        """
        if self.role == "user" and self.images:
            return {
                "role": "user",
                "content": self.content,
                "attachments": [
                    {
                        "id": image.id,
                        "name": image.name,
                        "mime": image.mime,
                        "width": image.width,
                        "height": image.height,
                    }
                    for image in self.images
                ],
            }
        return self.to_api()

    def to_record(self) -> dict[str, Any]:
        rec: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            rec["tool_calls"] = [call.to_record() for call in self.tool_calls]
        if self.tool_call_id:
            rec["tool_call_id"] = self.tool_call_id
        if self.name:
            rec["name"] = self.name
        if self.reasoning:
            rec["reasoning"] = self.reasoning
        return rec

    @classmethod
    def from_record(cls, rec: dict[str, Any]) -> "Message":
        return cls(
            role=str(rec.get("role") or "user"),
            content=str(rec.get("content") or ""),
            tool_calls=[ToolCall.from_record(c) for c in rec.get("tool_calls") or []],
            tool_call_id=rec.get("tool_call_id"),
            name=rec.get("name"),
            reasoning=rec.get("reasoning"),
        )


# --------------------------------------------------------------------------
# 用量
# --------------------------------------------------------------------------


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    cache_miss_tokens: int = 0

    @property
    def is_empty(self) -> bool:
        return not any(
            (self.prompt_tokens, self.completion_tokens, self.total_tokens)
        )

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.cached_tokens += other.cached_tokens
        self.cache_miss_tokens += other.cache_miss_tokens

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_tokens": self.cached_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
        }

    @classmethod
    def from_dict(cls, rec: dict[str, Any] | None) -> "Usage":
        rec = rec or {}
        return cls(
            prompt_tokens=int(rec.get("prompt_tokens") or 0),
            completion_tokens=int(rec.get("completion_tokens") or 0),
            total_tokens=int(rec.get("total_tokens") or 0),
            reasoning_tokens=int(rec.get("reasoning_tokens") or 0),
            cached_tokens=int(rec.get("cached_tokens") or 0),
            cache_miss_tokens=int(rec.get("cache_miss_tokens") or 0),
        )


# --------------------------------------------------------------------------
# 流式事件
# --------------------------------------------------------------------------


@dataclass
class ReasoningDelta:
    text: str


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolCallDelta:
    index: int
    id: str | None = None
    name: str | None = None
    arguments_fragment: str | None = None


@dataclass
class Finished:
    reason: str | None = None
    usage: Usage | None = None


StreamEvent = Union[ReasoningDelta, TextDelta, ToolCallDelta, Finished]


class StreamAssembler:
    """把碎片化的流式事件拼成一条完整的 assistant 消息。

    Provider 只负责忠实吐事件，拼装逻辑放在这里，
    这样 FakeProvider（测试）和真实 Provider 走完全相同的拼装路径。
    """

    def __init__(self) -> None:
        self._text: list[str] = []
        self._reasoning: list[str] = []
        self._calls: dict[int, ToolCall] = {}
        self.usage: Usage | None = None
        self.finish_reason: str | None = None
        self.event_count = 0

    def feed(self, event: StreamEvent) -> None:
        self.event_count += 1
        if isinstance(event, TextDelta):
            self._text.append(event.text)
        elif isinstance(event, ReasoningDelta):
            self._reasoning.append(event.text)
        elif isinstance(event, ToolCallDelta):
            call = self._calls.setdefault(event.index, ToolCall(id="", name=""))
            if event.id:
                call.id = event.id
            if event.name:
                call.name = event.name
            if event.arguments_fragment:
                call.arguments += event.arguments_fragment
        elif isinstance(event, Finished):
            if event.reason:
                self.finish_reason = event.reason
            if event.usage is not None:
                self.usage = event.usage

    @property
    def text(self) -> str:
        return "".join(self._text)

    @property
    def reasoning(self) -> str:
        return "".join(self._reasoning)

    def tool_calls(self) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for index in sorted(self._calls):
            call = self._calls[index]
            if not call.id:
                call.id = f"call_{index}"
            if not call.name:
                continue  # 没有名字的碎片无法执行，直接丢弃
            calls.append(call)
        return calls

    def message(self) -> Message:
        return Message(
            role="assistant",
            content=self.text,
            tool_calls=self.tool_calls(),
            reasoning=self.reasoning or None,
        )


# --------------------------------------------------------------------------
# 粗略 token 估算（仅用于事前裁剪；真实开销一律以 API 返回的 usage 为准）
# --------------------------------------------------------------------------

_CJK_RANGES = (
    (0x3000, 0x303F),  # CJK 标点
    (0x3040, 0x30FF),  # 日文假名
    (0x3400, 0x4DBF),  # 扩展 A
    (0x4E00, 0x9FFF),  # 基本汉字
    (0xF900, 0xFAFF),  # 兼容汉字
    (0xFF00, 0xFFEF),  # 全角字符
)


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """保守估算：中文按 1 字 1 token，其余按 4 字符 1 token（向上取整）。

    刻意偏保守 —— 宁可提前裁剪，也不要在真实请求上超预算。
    """
    if not text:
        return 0
    cjk = sum(1 for char in text if _is_cjk(char))
    other = len(text) - cjk
    return cjk + (other + 3) // 4


def estimate_image_tokens(width: int, height: int) -> int:
    """一张图大概多少 token（**预算用的估算**，真实开销一律以 API usage 为准）。

    公式由两次真实调用反推（2026-10-13 实测）：750×503 → 约 245 token，
    公式给 246；3000×2012 → 约 965 token，公式给上限 1024。
    折算下来就是 `像素 / 1536`，再把小图（官方会放大到约 544×544）和
    大图（等效上限约 1.69 MP）夹住。

    刻意**偏高估**：早一点触发压缩，比"上下文被图片悄悄顶爆"好得多。
    另外注意：图片**重复发送并不省 token** —— 实测第二次带图请求里图片那段
    并没有全额命中前缀缓存，所以每轮都会实打实地付这笔钱（只是每张只有几百）。
    """
    pixels = min(max(int(width) * int(height), 544 * 544), 1690000)
    return min(1024, max(193, round(pixels / 1536)))


def estimate_message_tokens(message: Message) -> int:
    total = estimate_tokens(message.content) + 4  # 4 为每条消息的结构开销
    # 思维链**也会真的发出去**（assistant 的 reasoning_content 要回传，见 Message.to_api）。
    # 以前不数它，工具循环里越长的思考越会让真实 prompt 比估算大 —— 而窗口是硬上限（ADR-100）。
    if message.reasoning:
        total += estimate_tokens(message.reasoning)
    for call in message.tool_calls:
        total += estimate_tokens(call.name) + estimate_tokens(call.arguments) + 8
    # tool 消息的 id/name 也在请求里；少算它们会低估，方向虽然不危险，但没必要留这个偏差。
    if message.tool_call_id:
        total += estimate_tokens(message.tool_call_id) + 4
    if message.name:
        total += estimate_tokens(message.name) + 2
    # 图片必须算进来：不算的话 auto-compact 永远不会触发，上下文会被图片顶爆。
    for image in message.images:
        total += estimate_image_tokens(image.width, image.height)
    return total


def estimate_tools_tokens(tools: Sequence[dict[str, Any]]) -> int:
    """工具定义本身占多少 token（它们每一轮都随请求发出去）。

    以前所有预算都只看"历史"，工具定义（本机实测 6 个约 1300 token、9 个约
    2000 token）完全在窗外。窗口小的时候这一块足以把请求顶出去 —— 而那时
    API 只会回一句 context length exceeded（ADR-100）。
    """
    items = list(tools or ())
    if not items:
        return 0
    # 按 JSON 文本估 + 每个工具一点结构开销：与真正发出去的形状一致。
    return estimate_tokens(json.dumps(items, ensure_ascii=False)) + 8 * len(items)


def estimate_messages_tokens(messages: list[Message]) -> int:
    return sum(estimate_message_tokens(message) for message in messages)


# --------------------------------------------------------------------------
# 消息分组
# --------------------------------------------------------------------------


def split_into_groups(messages: list[Message]) -> list[list[Message]]:
    """按 user 消息切分。一个"组"= 一条用户输入 + 它引发的所有 assistant/tool 消息。

    裁剪必须以组为单位：把 assistant 的 `tool_calls` 和对应的 `tool` 结果拆开，
    会造成 API 报错（tool 消息找不到它的调用者），这是 agent harness 的经典坑。

    放在 llm_types 而不是 context 里，是因为**事件重放（events.py）也要用它**：
    压缩后的"已覆盖多少"按**组数**记录比按消息条数稳 ——
    组内补一条 tool 结果（熔断/中断时会发生）不会让边界错位。
    """
    groups: list[list[Message]] = []
    current: list[Message] = []
    for message in messages:
        if message.role == "user" and current:
            groups.append(current)
            current = []
        current.append(message)
    if current:
        groups.append(current)
    return groups
