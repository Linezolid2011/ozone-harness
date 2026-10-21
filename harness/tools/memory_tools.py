"""记忆三件套工具：mem_create / mem_edit / mem_del。

这是模型**唯一**能改变外部世界的手段 —— 它没有文件系统权限，
对话历史的保存与记忆的注入都由 harness 服务端负责。

工具描述本身就是提示词工程：描述写得好不好，直接决定记忆质量
（会不会什么都记、会不会重复记、会不会忘记用 id）。

文案（描述、参数说明、回给模型的话、CLI 那一行简报）全在
`harness/tools/text_{zh,en}.py` 里，**每次实例化时**按当前语言取
（语言是运行期可切换的，不能在 import 时算好；见 `docs/12-i18n.md` §3）。
"""

from __future__ import annotations

from typing import Any

from ..i18n import t
from ..memory import MemoryStore
from .registry import ToolResult


class MemCreateTool:
    name = "mem_create"

    def __init__(self, store: MemoryStore) -> None:
        self._store = store
        self.description = t("tool.mem_create.desc")
        self.parameters = {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": t("tool.mem_create.param.content"),
                },
                # category 的说明与 mem_edit 共用一条（同一句话只留一位主人）
                "category": {"type": "string", "description": t("tool.common.category_hint")},
            },
            "required": ["content"],
        }

    def run(self, args: dict[str, Any]) -> ToolResult:
        payload = self._store.create(
            content=str(args.get("content") or ""),
            category=args.get("category"),
        )
        if payload.get("ok"):
            display = t(
                "tool.mem_create.display.created",
                id=payload["id"],
                content=payload["content"],
            )
        else:
            display = t("tool.mem_create.display.failed", error=payload.get("error"))
        return ToolResult(ok=bool(payload.get("ok")), payload=payload, display=display)


class MemEditTool:
    name = "mem_edit"

    def __init__(self, store: MemoryStore) -> None:
        self._store = store
        self.description = t("tool.mem_edit.desc")
        self.parameters = {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": t("tool.mem_edit.param.id")},
                "content": {"type": "string", "description": t("tool.mem_edit.param.content")},
                # 与 mem_create 共用一条（见那里的注释）
                "category": {"type": "string", "description": t("tool.common.category_hint")},
            },
            "required": ["id"],
        }

    def run(self, args: dict[str, Any]) -> ToolResult:
        raw_id = args.get("id")
        try:
            memory_id = int(raw_id)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return ToolResult(
                ok=False,
                payload={"error": t("tool.common.bad_id", raw_id=raw_id)},
                display=t("tool.common.bad_id.display"),
            )
        payload = self._store.edit(
            memory_id,
            content=args.get("content"),
            category=args.get("category"),
        )
        if payload.get("ok"):
            display = t(
                "tool.mem_edit.display.updated",
                id=memory_id,
                content=payload.get("content"),
            )
        else:
            display = t("tool.mem_edit.display.failed", error=payload.get("error"))
        return ToolResult(ok=bool(payload.get("ok")), payload=payload, display=display)


class MemDeleteTool:
    name = "mem_del"

    def __init__(self, store: MemoryStore) -> None:
        self._store = store
        self.description = t("tool.mem_del.desc")
        self.parameters = {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": t("tool.mem_del.param.id")},
                "reason": {"type": "string", "description": t("tool.mem_del.param.reason")},
            },
            "required": ["id"],
        }

    def run(self, args: dict[str, Any]) -> ToolResult:
        raw_id = args.get("id")
        try:
            memory_id = int(raw_id)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return ToolResult(
                ok=False,
                payload={"error": t("tool.common.bad_id", raw_id=raw_id)},
                display=t("tool.common.bad_id.display"),
            )
        payload = self._store.delete(memory_id, reason=args.get("reason"))
        if payload.get("ok"):
            display = t(
                "tool.mem_del.display.deleted",
                id=memory_id,
                forgot=payload.get("forgot"),
            )
        else:
            display = t("tool.mem_del.display.failed", error=payload.get("error"))
        return ToolResult(ok=bool(payload.get("ok")), payload=payload, display=display)
