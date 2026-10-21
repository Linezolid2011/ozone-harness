"""工具层：注册表与工具协议。

关键约定：工具执行**永不抛异常到循环里**。
任何失败（未知工具、JSON 不合法、参数错误、内部崩溃）都变成一条
ok=false 的工具结果回喂给模型，让它自己纠正 —— 这是 agent 循环稳定的前提。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..i18n import t


def _string_paths(node: Any, prefix: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """找出结构里所有字符串值的"位置"（键 / 下标组成的路径）。

    **为什么要先找位置**：payload 经常是嵌套的（搜索结果就是"列表里套字典"），
    只看最外层字符串根本救不了一个被切碎的 web_search 结果。
    """
    found: list[tuple[Any, ...]] = []
    if isinstance(node, str):
        found.append(prefix)
    elif isinstance(node, dict):
        for key, value in node.items():
            found.extend(_string_paths(value, prefix + (key,)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_string_paths(value, prefix + (index,)))
    return found


def _get_at(node: Any, path: tuple[Any, ...]) -> Any:
    for step in path:
        node = node[step]
    return node


def _set_at(node: Any, path: tuple[Any, ...], value: Any) -> None:
    parent = node if len(path) == 1 else _get_at(node, path[:-1])
    parent[path[-1]] = value


def _dump(record: Any) -> str:
    return json.dumps(record, ensure_ascii=False)


def _truncate_json(
    record: dict[str, Any], *, max_chars: int, original_chars: int
) -> str:
    """结构化截断：**保住每一个键**，只把最长的字符串值截短。

    旧做法是把整段 JSON 按字符切（`text[:max_chars - 80]`），后果有三条：
      1. 正文停在半句话中间 —— 抓 JSON 接口时，"正文"本身就是一份被切碎的
         JSON（实测 8000 汉字的正文渲染后只剩 1989 字符），模型没法解析它；
      2. 更糟的是排在后面的小字段（note / url / content_type…）整块消失，
         模型看不到"为什么被截断"，只会以为页面本来就这么短；
      3. 于是 net/config.py 里写的 8000 永远不生效 —— 实际被这里按 2000 切掉。

    所以这里改成"按结构截"：键全留，值从最长的字符串开始砍到放得下为止，
    再补上 `truncated: true` 与 `truncated_chars`（截断前**整段 JSON** 的字符数），
    让模型知道"内容被截了、原来有多长"。
    """
    trimmed = dict(record)
    trimmed["truncated"] = True
    trimmed["truncated_chars"] = original_chars
    paths = sorted(
        _string_paths(trimmed), key=lambda path: len(_get_at(trimmed, path)), reverse=True
    )
    for path in paths:
        if len(_dump(trimmed)) <= max_chars:
            break
        original = _get_at(trimmed, path)
        if not original:
            continue
        # 二分出"这个键最多还能留几个字符"：前缀越长 JSON 一定越长（单调），
        # 所以二分一定收敛 —— 极端小的上限也不会转不出来。
        low, high = 0, len(original)
        while low < high:
            mid = (low + high + 1) // 2
            _set_at(trimmed, path, original[:mid])
            if len(_dump(trimmed)) <= max_chars:
                low = mid
            else:
                high = mid - 1
        _set_at(trimmed, path, original[:low])

    text = _dump(trimmed)
    if len(text) <= max_chars:
        return text
    # 走到这里说明"连键名本身都放不下"（比如上限只有 20）。
    # 此时只保证两件事：**永远返回合法 JSON**、模型知道"被截断了"。
    minimal: dict[str, Any] = {
        "ok": bool(record.get("ok")),
        "truncated": True,
        "truncated_chars": original_chars,
    }
    text = _dump(minimal)
    if len(text) <= max_chars:
        return text
    return _dump({"ok": bool(record.get("ok")), "truncated": True})


@dataclass
class ToolResult:
    ok: bool
    payload: dict[str, Any] = field(default_factory=dict)
    display: str = ""  # 给 CLI 看的一行简报

    def as_tool_content(self, max_chars: int) -> str:
        record: dict[str, Any] = {"ok": self.ok}
        record.update(self.payload)
        text = _dump(record)
        if len(text) <= max_chars:
            return text
        return _truncate_json(record, max_chars=max_chars, original_chars=len(text))


class Tool(Protocol):
    name: str
    description: str
    parameters: dict[str, Any]

    def run(self, args: dict[str, Any]) -> ToolResult:
        ...


class ToolRegistry:
    def __init__(self, max_result_chars: int = 2000) -> None:
        self._tools: dict[str, Tool] = {}
        self._max_result_chars = max_result_chars

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[dict[str, Any]]:
        """按名字排序输出，保证 tools 数组在每轮请求里字节一致（利于命中缓存）。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in (self._tools[name] for name in self.names())
        ]

    def call(self, name: str, arguments: str) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            # 注册表自己产出的错误也是**模型看到的文字**，一律现取（`docs/12-i18n.md` §1）。
            message = t("tool.registry.unknown", name=name)
            return ToolResult(
                ok=False,
                payload={"error": message, "available_tools": self.names()},
                display=message,
            )

        text = (arguments or "").strip()
        if not text:
            args: Any = {}
        else:
            try:
                args = json.loads(text)
            except json.JSONDecodeError as exc:
                return ToolResult(
                    ok=False,
                    payload={
                        "error": t("tool.registry.bad_json", msg=exc.msg),
                        "received": text[:200],
                    },
                    display=t("tool.registry.bad_json.display"),
                )
        if not isinstance(args, dict):
            return ToolResult(
                ok=False,
                payload={"error": t("tool.registry.not_object")},
                display=t("tool.registry.not_object.display"),
            )

        try:
            return tool.run(args)
        except Exception as exc:  # noqa: BLE001 - 工具崩溃不应中断对话
            return ToolResult(
                ok=False,
                payload={
                    "error": t(
                        "tool.registry.crashed",
                        kind=type(exc).__name__,
                        message=exc,
                    )
                },
                display=t("tool.registry.crashed.display"),
            )

    def render_content(self, result: ToolResult) -> str:
        return result.as_tool_content(self._max_result_chars)
