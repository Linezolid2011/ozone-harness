"""背景档案的只读工具：dream_list / dream_read / dream_search。

这是"README 只是投影，细节要靠模型自己走进去找"这一设计的落地点。
Kimi 的原话是"如果它不在注入块里、而且我这个会话里没有用工具读过 vault 文件，
那我就是不知道" —— 那就是这三个工具存在的理由。

三条设计约束：

1. **只读**。模型改变世界的唯一手段仍然是记忆三件套（ADR-002 的那条边界没有动）。
   vault 由 dream agent 写，对话 agent 只能看 —— 这样"用户明说的"与"后台推断的"
   在**权限层面**也是分开的，不只是提示词里的一句话。
2. **路径校验在 `VaultStore` 里**，不在这里。工具层只负责把异常翻译成
   模型看得懂的一句话，所以将来多一个写入口也不会绕过那套校验。
3. **结果要小**。工具结果的上限是 2000 字符（registry 的结构化截断），
   而"列出 40 个文件"很容易超过它 —— 被截断之后每个摘要都只剩几个字，
   模型反而更糊涂。所以这里自己先按条数和字段长度收敛，并如实报告"还有多少没显示"。
"""

from __future__ import annotations

import json
from typing import Any

from ..i18n import t
from ..vault import README_NAME, VaultPathError, VaultStore
from .registry import ToolResult


def _path_hint() -> str:
    """路径提示。**现取**：它跟着界面语言走，不能在 import 时算好。"""
    return t("tool.dream_read.path_hint", readme=README_NAME)


# 给元信息、邻域、键名与转义留的余量。读取工具用它反推"正文最多能有多少字符"。
_READ_OVERHEAD = 900
# 目录列表最多占多少余量（`more` / `note` 两个键）。
_LIST_RESERVE = 200


def _friendly_error(exc: Exception) -> ToolResult:
    if isinstance(exc, FileNotFoundError):
        name = exc.args[0] if exc.args else ""
        return ToolResult(
            ok=False,
            payload={
                "error": t("tool.dream_read.missing", name=name),
                "hint": _path_hint(),
            },
            display=t("tool.dream_read.missing.display", name=name),
        )
    if isinstance(exc, VaultPathError):
        # `str(exc)` 由**档案层**（harness/vault.py，`ui.vault.*`）自己按当前语言取 ——
        # 这里只把异常转成一句话，不复制那份文案
        return ToolResult(
            ok=False,
            payload={"error": str(exc), "hint": _path_hint()},
            display=t("tool.dream_read.bad_path.display"),
        )
    raise exc


class VaultListTool:
    name = "dream_list"

    def __init__(self, store: VaultStore, *, max_result_chars: int = 2000) -> None:
        self._store = store
        self.description = t("tool.dream_list.desc")
        self.parameters = {
            "type": "object",
            "properties": {
                "prefix": {
                    "type": "string",
                    "description": t("tool.dream_list.param.prefix"),
                }
            },
            "required": [],
        }
        # **工具结果的真实上限是 min(自己的设置, 循环的结果上限)**。
        # 只看自己那一个数字，就会出现"设了 8000、实际被 2000 砍掉"——
        # 联网抓取已经踩过这个坑（ADR-051），这里直接沿用它的做法：
        # 把上限显式传进来，由工具自己按预算收敛，而不是让它事后被砍。
        self._budget = max(400, int(max_result_chars))

    def run(self, args: dict[str, Any]) -> ToolResult:
        prefix = str(args.get("prefix") or "")
        entries = self._store.list_files(prefix)
        payload: dict[str, Any] = {"count": len(entries), "files": []}
        for entry in entries:
            payload["files"].append(
                {
                    "path": entry.path,
                    "title": entry.title[:30],
                    "kind": entry.kind,
                    "summary": entry.summary[:50],
                }
            )
            rendered = len(json.dumps(payload, ensure_ascii=False))
            if rendered > self._budget - _LIST_RESERVE:
                payload["files"].pop()
                break
        if len(payload["files"]) < len(entries):
            payload["more"] = len(entries) - len(payload["files"])
            payload["note"] = t("tool.dream_list.more")
        if not entries:
            payload["note"] = (
                t("tool.dream_list.empty")
                if self._store.exists()
                else t("tool.dream_list.no_vault")
            )
        return ToolResult(
            ok=True,
            payload=payload,
            display=t("tool.dream_list.display", count=len(entries)),
        )


class VaultReadTool:
    name = "dream_read"

    def __init__(self, store: VaultStore, *, max_result_chars: int = 2000) -> None:
        self._store = store
        self.description = t("tool.dream_read.desc")
        self.parameters = {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": t("tool.dream_read.param.path"),
                },
                "max_chars": {
                    "type": "integer",
                    "description": t("tool.dream_read.param.max_chars"),
                },
            },
            "required": ["path"],
        }
        self._budget = max(400, int(max_result_chars))

    def _body_cap(self) -> int:
        """正文最多能有多少字符，才能让整个 JSON 结果放得下。"""
        return max(400, min(self._store.read_max_chars, self._budget - _READ_OVERHEAD))

    def run(self, args: dict[str, Any]) -> ToolResult:
        raw = str(args.get("path") or "").strip()
        if not raw:
            return ToolResult(
                ok=False,
                payload={
                    "error": t("tool.common.path_required"),
                    "hint": _path_hint(),
                },
                display=t("tool.dream_read.empty_path.display"),
            )
        cap = self._body_cap()
        wanted = self._store.read_max_chars
        limit = args.get("max_chars")
        if limit is not None:
            try:
                wanted = max(0, int(limit))
            except (TypeError, ValueError):
                return ToolResult(
                    ok=False,
                    payload={
                        "error": t("tool.dream_read.bad_max_chars", limit=limit)
                    },
                    display=t("tool.dream_read.bad_max_chars.display"),
                )
        effective = min(wanted, cap)
        try:
            file = self._store.read(raw, max_chars=effective)
        except Exception as exc:  # noqa: BLE001 - 翻译成模型看得懂的话
            return _friendly_error(exc)

        try:
            neighbors = self._store.neighborhood(file.path)
        except (VaultPathError, OSError):
            neighbors = {"links": [], "backlinks": [], "related": []}

        payload: dict[str, Any] = {
            "path": file.path,
            "frontmatter": file.frontmatter.to_dict(),
            "body": file.body,
            # `chars` 是**磁盘上的真实长度**，`returned_chars` 是这次给了多少。
            # 只给前者（第一版给的是截断后的长度）会让模型完全无法判断
            # "这个文件到底有多大、我还差多少没看到"。
            "chars": file.total_chars,
            "returned_chars": len(file.body),
            "neighbors": neighbors,
        }
        if file.truncated:
            payload["truncated"] = True
            # 两种截断要说清楚是哪一种：文件本身很长（可以再读一点），
            # 还是被工具结果上限卡住了（再要也拿不到）—— 否则模型会一直重试。
            if effective < wanted:
                payload["note"] = t(
                    "tool.dream_read.truncated_by_budget",
                    total=file.total_chars,
                    returned=len(file.body),
                )
            else:
                payload["note"] = t(
                    "tool.dream_read.truncated_by_max_chars",
                    total=file.total_chars,
                    returned=len(file.body),
                )
        if file.warnings:
            payload["warnings"] = file.warnings[:3]
        return ToolResult(
            ok=True,
            payload=payload,
            display=t("tool.dream_read.display", path=file.path, chars=file.chars),
        )


class VaultSearchTool:
    name = "dream_search"

    def __init__(self, store: VaultStore, *, max_result_chars: int = 2000) -> None:
        self._store = store
        self.description = t("tool.dream_search.desc")
        self.parameters = {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": t("tool.dream_search.param.query"),
                },
                "limit": {
                    "type": "integer",
                    "description": t("tool.dream_search.param.limit"),
                },
            },
            "required": ["query"],
        }
        self._budget = max(400, int(max_result_chars))

    def run(self, args: dict[str, Any]) -> ToolResult:
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult(
                ok=False,
                payload={"error": t("tool.dream_search.empty_query")},
                display=t("tool.dream_search.empty_query.display"),
            )
        limit = args.get("limit")
        if limit is None:
            wanted = 8
        else:
            try:
                wanted = max(1, min(20, int(limit)))
            except (TypeError, ValueError):
                return ToolResult(
                    ok=False,
                    payload={"error": t("tool.dream_search.bad_limit", limit=limit)},
                    display=t("tool.dream_search.bad_limit.display"),
                )

        hits = self._store.search(query, limit=wanted)
        payload: dict[str, Any] = {"query": query, "count": len(hits), "hits": []}
        # 与 dream_list 同样按预算收敛：**工具自己先收**，而不是让 registry 事后砍。
        # 事后砍会把每条命中的 snippet 一起削短，模型反而更看不出哪条相关。
        for hit in hits:
            payload["hits"].append(hit)
            if len(json.dumps(payload, ensure_ascii=False)) > self._budget - _LIST_RESERVE:
                payload["hits"].pop()
                break
        if len(payload["hits"]) < len(hits):
            payload["more"] = len(hits) - len(payload["hits"])
            payload["note"] = t("tool.dream_search.more")
        elif not hits:
            payload["note"] = t("tool.dream_search.no_hits")
        return ToolResult(
            ok=True,
            payload=payload,
            display=t("tool.dream_search.display", query=query, count=len(hits)),
        )
