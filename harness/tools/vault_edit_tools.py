"""会话 agent 的背景档案编辑工具（ADR-111）。

这些类是模型唯一能写 dream vault 的入口。它们不接受任意文件系统操作，
只调用 VaultStore 的 session 方法；路径白名单、生成物/独立记忆保护、
固定 section 删除保护、符号链接检查与生成物重建全部在 store 层。

与 dream 整理器的 `vault_write` 分开：create/update 是两种显式操作，
避免模型拼错路径时把"编辑"变成新建，或把已有档案无意覆盖。
"""

from __future__ import annotations

from typing import Any

from ..i18n import t
from ..vault import Frontmatter, VaultPathError, VaultStore
from .registry import ToolResult


def _path_hint() -> str:
    return t("tool.dream_edit.path_hint")


def _list_arg(args: dict[str, Any], key: str) -> tuple[str, ...]:
    value = args.get(key)
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, str) and value.strip():
        return tuple(part.strip() for part in value.split(",") if part.strip())
    return ()


def _frontmatter(args: dict[str, Any]) -> Frontmatter:
    """与整理工具同口径：标量字段 + 自定义 metadata；结构化 relation 走 extra。"""
    metadata = args.get("metadata")
    known = {
        "name", "title", "kind", "relation", "status", "created",
        "last_updated", "updated", "place", "tags", "aliases", "sources",
    }
    extra = (
        {
            str(key): value
            for key, value in metadata.items()
            if str(key) not in known
        }
        if isinstance(metadata, dict)
        else {}
    )
    relation = args.get("relation")
    if isinstance(relation, (list, dict)):
        extra["relation"] = relation
        relation_text = ""
    else:
        relation_text = str(relation or "").strip()
    return Frontmatter(
        name=str(args.get("name") or "").strip(),
        title=str(args.get("title") or "").strip(),
        kind=str(args.get("kind") or "").strip(),
        relation=relation_text,
        status=str(args.get("status") or "").strip(),
        place=str(args.get("place") or "").strip(),
        tags=_list_arg(args, "tags"),
        aliases=_list_arg(args, "aliases"),
        extra=extra,
    )


def _write_error(exc: Exception, raw: str) -> ToolResult:
    if isinstance(exc, FileNotFoundError):
        message = t("tool.common.archive_missing", raw=raw)
        return ToolResult(False, {"error": message, "hint": _path_hint()}, message)
    if isinstance(exc, VaultPathError):
        return ToolResult(
            False,
            {"error": str(exc), "hint": _path_hint()},
            t("tool.common.write_rejected.display"),
        )
    return ToolResult(
        False,
        {"error": t("tool.common.write_failed", error=exc)},
        t("tool.common.write_failed.display"),
    )


def _write_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": _path_hint()},
            "body": {"type": "string", "description": t("tool.vault_write.param.body")},
            "name": {"type": "string", "description": t("tool.vault_write.param.name")},
            "title": {"type": "string", "description": t("tool.vault_write.param.title")},
            "kind": {"type": "string", "description": t("tool.vault_write.param.kind")},
            "relation": {
                "type": "string",
                "description": t("tool.vault_write.param.relation"),
            },
            "status": {"type": "string", "description": t("tool.vault_write.param.status")},
            "place": {"type": "string", "description": t("tool.vault_write.param.place")},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": t("tool.vault_write.param.tags"),
            },
            "aliases": {
                "type": "array",
                "items": {"type": "string"},
                "description": t("tool.vault_write.param.aliases"),
            },
            "metadata": {
                "type": "object",
                "description": t("tool.vault_write.param.metadata"),
            },
        },
        "required": ["path", "body"],
    }


class _DreamSaveTool:
    mode = ""

    def __init__(self, store: VaultStore) -> None:
        self._store = store
        self.description = t(f"tool.{self.name}.desc")
        self.parameters = _write_parameters()

    def run(self, args: dict[str, Any]) -> ToolResult:
        raw = str(args.get("path") or "").strip()
        if not raw:
            return ToolResult(
                False,
                {"error": t("tool.common.path_required"), "hint": _path_hint()},
                t("tool.common.path_missing.display"),
            )
        if "body" not in args:
            return ToolResult(
                False,
                {"error": t("tool.vault_write.empty_body")},
                t("tool.vault_write.empty_body.display"),
            )
        body = str(args.get("body") or "").strip()
        # 新建空档案没有信息价值；编辑则允许清空正文（固定 section 也只能这样清空）。
        if self.mode == "create" and not body:
            return ToolResult(
                False,
                {"error": t("tool.vault_write.empty_body")},
                t("tool.vault_write.empty_body.display"),
            )
        try:
            result = self._store.save_session_note(
                raw, body, _frontmatter(args), mode=self.mode
            )
        except (VaultPathError, OSError) as exc:
            return _write_error(exc, raw)
        return ToolResult(
            True,
            {
                "path": result["path"],
                "action": result["action"],
                "frontmatter": result["frontmatter"],
                "body_chars": result["body_chars"],
                "regenerated": bool(result["generated"].get("rebuilt")),
            },
            t(
                f"tool.{self.name}.display",
                path=result["path"],
                chars=result["body_chars"],
            ),
        )


class DreamCreateTool(_DreamSaveTool):
    name = "dream_create"
    mode = "create"


class DreamEditTool(_DreamSaveTool):
    name = "dream_edit"
    mode = "update"


class DreamMkdirTool:
    name = "dream_mkdir"

    def __init__(self, store: VaultStore) -> None:
        self._store = store
        self.description = t("tool.dream_mkdir.desc")
        self.parameters = {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": _path_hint()},
            },
            "required": ["path"],
        }

    def run(self, args: dict[str, Any]) -> ToolResult:
        raw = str(args.get("path") or "").strip()
        if not raw:
            return ToolResult(
                False,
                {"error": t("tool.common.path_required"), "hint": _path_hint()},
                t("tool.common.path_missing.display"),
            )
        try:
            result = self._store.create_session_directory(raw)
        except (VaultPathError, OSError) as exc:
            return _write_error(exc, raw)
        return ToolResult(
            True,
            {
                "path": result["path"],
                "action": result["action"],
                "regenerated": bool(result["generated"].get("rebuilt")),
            },
            t(f"tool.dream_mkdir.display.{result['action']}", path=result["path"]),
        )


class DreamDeleteTool:
    name = "dream_delete"

    def __init__(self, store: VaultStore) -> None:
        self._store = store
        self.description = t("tool.dream_delete.desc")
        self.parameters = {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": _path_hint()},
                "reason": {
                    "type": "string",
                    "description": t("tool.dream_delete.param.reason"),
                },
                "recursive": {
                    "type": "boolean",
                    "description": t("tool.dream_delete.param.recursive"),
                },
            },
            "required": ["path"],
        }

    def run(self, args: dict[str, Any]) -> ToolResult:
        raw = str(args.get("path") or "").strip()
        if not raw:
            return ToolResult(
                False,
                {"error": t("tool.common.path_required"), "hint": _path_hint()},
                t("tool.common.path_missing.display"),
            )
        recursive = args.get("recursive", False)
        if not isinstance(recursive, bool):
            return ToolResult(
                False,
                {"error": t("tool.dream_delete.bad_recursive")},
                t("tool.dream_delete.bad_recursive.display"),
            )
        reason = str(args.get("reason") or "").strip()
        try:
            result = self._store.delete_session_path(raw, recursive=recursive)
        except FileNotFoundError:
            message = t("tool.common.archive_missing", raw=raw)
            return ToolResult(False, {"error": message}, message)
        except VaultPathError as exc:
            return ToolResult(
                False,
                {"error": str(exc), "hint": _path_hint()},
                t("tool.dream_delete.rejected.display"),
            )
        except OSError as exc:
            return ToolResult(
                False,
                {"error": t("tool.common.write_failed", error=exc)},
                t("tool.common.write_failed.display"),
            )
        return ToolResult(
            True,
            {**result, "reason": reason},
            t(
                f"tool.dream_delete.display.{result['kind']}",
                path=result["path"],
                count=result["files"],
            ),
        )


def build_session_vault_edit_tools(store: VaultStore) -> tuple[Any, ...]:
    """固定顺序供 Runtime 与提示词只读视图共用，避免两处定义漂移。"""
    return (
        DreamCreateTool(store),
        DreamEditTool(store),
        DreamMkdirTool(store),
        DreamDeleteTool(store),
    )
