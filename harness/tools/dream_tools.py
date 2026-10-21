"""dream agent 专用的工具集（M7 整理流水线）。

与对话 agent 的工具**故意分开**，而且这次是**彻底分开**：

| | 对话 agent | dream agent |
|---|---|---|
| 记忆条目 | `mem_create / mem_edit / mem_del`（可以写） | **一个都没有**（连读都没有） |
| 背景档案 | `dream_list / dream_read / dream_search`（只读） | `vault_*` / `readme_write`（可以写） |
| 会话记录 | 看不到（它只活在当前会话里） | `records_*`（只读） |

这条边界是 ADR-053 的落地点：**记录者与整理者互不越界**。整理者碰不到
"用户明确要求记住"的东西，所以它不可能改写历史；记录者碰不到档案文件，
所以它不可能把一次性的猜测固化进长期档案。

> 这里以前是记忆手术工具（`mem_merge` / `mem_rewrite` / `mem_forget` /
> `mem_insight`），随 ADR-059 整体退役 —— 那些工具的共同问题是**作用对象错了**：
> 它们在改"用户明说的事实"，而不是"后台整理的投影"。

## 三个容易写错的地方

1. **记录是分页读的，而且只有"连续读到的部分"能推进游标。**
   `records_read` 会回报 `next_seq`，但真正决定游标的是 `ReadTracker`
   （见 `dream_records.py`）：跳读留下的空洞必须留到下次再读，
   否则那几条记录会被永久跳过。
2. **`vault_write` 是"合并式写入"**：只覆盖模型明确给出的 frontmatter 字段，
   你没提到的元信息（含你手工加的键）原样保留。它还会**由 harness 盖 `updated` 日期** ——
   模型不知道今天几号，让它写日期等于让它编。
3. **`readme_write` 会当场告诉模型超没超预算**。README 每轮都进对话模型的上下文，
   有硬上限（默认 1200 token）。让它在写完的**同一步**就知道"太长了"，
   比等到下一轮整理时才发现便宜得多。
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Mapping

from ..dream_records import ReadTracker, read_session
from ..i18n import t
from ..llm_types import estimate_tokens
from ..vault import (
    PLACEHOLDER_STATUS,
    README_NAME,
    Frontmatter,
    VaultPathError,
    VaultStore,
    dump_frontmatter,
    merge_frontmatter,
    parse_frontmatter,
)
from .registry import ToolResult


def _path_hint() -> str:
    """路径提示。**现取**：跟着当前语言走，不能在 import 时算好。"""
    return t("tool.common.vault_path_hint", readme=README_NAME)


# 工具结果里给列表/检索留的余量（与 vault_tools 同一套做法）。
_LIST_RESERVE = 200


class RecordsListTool:
    name = "records_list"

    def __init__(self, sessions_dir, cursors: Mapping[str, int], *, max_result_chars: int = 2000) -> None:
        self._sessions_dir = sessions_dir
        self._cursors = dict(cursors)
        self.description = t("tool.records_list.desc")
        self.parameters = {"type": "object", "properties": {}, "required": []}
        self._budget = max(400, int(max_result_chars))

    def run(self, args: dict[str, Any]) -> ToolResult:
        from ..dream_records import pending_sessions

        items = pending_sessions(self._sessions_dir, self._cursors)
        payload: dict[str, Any] = {"count": len(items), "sessions": []}
        for item in items:
            payload["sessions"].append(
                {
                    "session_id": item.session_id,
                    "title": item.title[:60],
                    "new_events": item.new_events,
                    "turns": item.total_turns,
                }
            )
            if len(json.dumps(payload, ensure_ascii=False)) > self._budget - _LIST_RESERVE:
                payload["sessions"].pop()
                payload["more"] = len(items) - len(payload["sessions"])
                break
        if not items:
            payload["note"] = t("tool.records_list.empty")
        return ToolResult(
            True, payload, t("tool.records_list.display", count=len(items))
        )


class RecordsReadTool:
    name = "records_read"

    def __init__(
        self,
        sessions_dir,
        cursors: Mapping[str, int],
        tracker: ReadTracker,
        *,
        max_events: int = 40,
        max_chars: int = 6000,
        max_result_chars: int = 2000,
    ) -> None:
        self._sessions_dir = sessions_dir
        self._cursors = dict(cursors)
        self._tracker = tracker
        self.description = t("tool.records_read.desc")
        self.parameters = {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": t("tool.records_read.param.session_id"),
                },
                "start_seq": {
                    "type": "integer",
                    "description": t("tool.records_read.param.start_seq"),
                },
                "max_events": {
                    "type": "integer",
                    "description": t("tool.records_read.param.max_events"),
                },
                "max_chars": {
                    "type": "integer",
                    "description": t("tool.records_read.param.max_chars"),
                },
            },
            "required": ["session_id"],
        }
        self._max_events = max(1, int(max_events))
        self._max_chars = max(400, int(max_chars))
        self._budget = max(400, int(max_result_chars))

    def run(self, args: dict[str, Any]) -> ToolResult:
        session_id = str(args.get("session_id") or "").strip()
        if not session_id:
            return ToolResult(
                False,
                {"error": t("tool.records_read.empty_session")},
                t("tool.records_read.empty_session.display"),
            )
        # 会话 id 直接来自文件名：只接受"文件名该有的样子"，不接受任何路径成分
        if session_id != session_id.replace("/", "").replace("\\", "") or session_id in (".", ".."):
            return ToolResult(
                False,
                {
                    "error": t("tool.records_read.bad_session", session_id=session_id),
                    "hint": t("tool.records_read.bad_session.hint"),
                },
                t("tool.records_read.bad_session.display"),
            )
        path = self._sessions_dir / f"{session_id}.jsonl"
        if not path.is_file():
            return ToolResult(
                False,
                {
                    "error": t("tool.records_read.no_session", session_id=session_id),
                    "hint": t("tool.records_read.no_session.hint"),
                },
                t("tool.records_read.no_session.display"),
            )

        cursor = int(self._cursors.get(session_id, 0))
        start_seq = args.get("start_seq")
        if start_seq is None:
            start_seq = cursor + 1
        try:
            start_seq = max(1, int(start_seq))
        except (TypeError, ValueError):
            return ToolResult(
                False,
                {"error": t("tool.records_read.bad_start_seq", start_seq=start_seq)},
                t("tool.records_read.bad_start_seq.display"),
            )

        max_events = args.get("max_events")
        max_chars = args.get("max_chars")
        try:
            limit_events = max(1, int(max_events)) if max_events is not None else self._max_events
            limit_chars = max(400, int(max_chars)) if max_chars is not None else self._max_chars
        except (TypeError, ValueError):
            return ToolResult(
                False,
                {"error": t("tool.records_read.bad_paging")},
                t("tool.records_read.bad_paging.display"),
            )
        # 单次返回不能超过工具结果上限（同 vault_tools 的教训：自己先收，别让 registry 事后砍）
        limit_chars = min(limit_chars, max(400, self._budget - 400))

        rendered = read_session(
            path, start_seq=start_seq, max_events=limit_events, max_chars=limit_chars
        )
        self._tracker.note(session_id, rendered.from_seq, rendered.to_seq)
        payload: dict[str, Any] = {
            "session_id": session_id,
            "from_seq": rendered.from_seq,
            "to_seq": rendered.to_seq,
            "rendered": rendered.rendered,
            "remaining": rendered.remaining,
            "text": rendered.text,
        }
        if rendered.remaining:
            payload["note"] = t(
                "tool.records_read.more",
                remaining=rendered.remaining,
                next_seq=rendered.to_seq + 1,
            )
        elif not rendered.text.strip():
            payload["note"] = t("tool.records_read.empty")
        if rendered.warnings:
            # 单条被截断、序号有缺口 —— 都必须说出来，否则模型会以为它看全了
            payload["warnings"] = rendered.warnings
        return ToolResult(
            True,
            payload,
            t("tool.records_read.display", session_id=session_id, count=rendered.rendered),
        )


class VaultOutlineTool:
    name = "vault_outline"

    def __init__(self, store: VaultStore, *, max_result_chars: int = 2000) -> None:
        self._store = store
        self.description = t("tool.vault_outline.desc")
        self.parameters = {
            "type": "object",
            "properties": {
                "prefix": {
                    "type": "string",
                    "description": t("tool.vault_outline.param.prefix"),
                }
            },
            "required": [],
        }
        self._budget = max(400, int(max_result_chars))

    def run(self, args: dict[str, Any]) -> ToolResult:
        prefix = str(args.get("prefix") or "")
        entries = self._store.list_files(prefix)
        readme, truncated = self._store.readme_text()
        payload: dict[str, Any] = {
            "files": [],
            "readme_tokens": estimate_tokens(readme),
            "readme_budget": self._store.readme_token_budget,
            "readme_truncated": truncated,
        }
        if not self._store.exists():
            payload["note"] = t("tool.vault_outline.first_run")
        for entry in entries:
            payload["files"].append(
                {
                    "path": entry.path,
                    "title": entry.title[:40],
                    "kind": entry.kind,
                    "chars": entry.chars,
                    "summary": entry.summary[:60],
                }
            )
            if len(json.dumps(payload, ensure_ascii=False)) > self._budget - _LIST_RESERVE:
                payload["files"].pop()
                payload["more"] = len(entries) - len(payload["files"])
                break
        if truncated:
            payload["note"] = t("tool.vault_outline.readme_over_budget")
        return ToolResult(
            True, payload, t("tool.vault_outline.display", count=len(entries))
        )


class VaultReadTool:
    name = "vault_read"

    def __init__(self, store: VaultStore, *, max_result_chars: int = 2000) -> None:
        self._store = store
        self.description = t("tool.vault_read.desc")
        self.parameters = {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": _path_hint()},
                "max_chars": {
                    "type": "integer",
                    "description": t("tool.vault_read.param.max_chars"),
                },
            },
            "required": ["path"],
        }
        self._budget = max(400, int(max_result_chars))

    def run(self, args: dict[str, Any]) -> ToolResult:
        raw = str(args.get("path") or "").strip()
        if not raw:
            return ToolResult(
                False,
                {"error": t("tool.common.path_required")},
                t("tool.common.path_missing.display"),
            )
        cap = max(400, min(self._store.read_max_chars, self._budget - 700))
        limit = args.get("max_chars")
        try:
            wanted = min(int(limit), cap) if limit is not None else cap
        except (TypeError, ValueError):
            return ToolResult(
                False,
                {"error": t("tool.vault_read.bad_max_chars", limit=limit)},
                t("tool.vault_read.bad_max_chars.display"),
            )
        try:
            file = self._store.read(raw, max_chars=max(0, wanted))
        except FileNotFoundError:
            missing = t("tool.common.archive_missing", raw=raw)
            return ToolResult(
                False,
                {"error": missing, "hint": _path_hint()},
                missing,
            )
        except VaultPathError as exc:
            # `str(exc)` 由**档案层**（harness/vault.py，`ui.vault.*`）自己按当前语言取，
            # 这里不复制那份文案
            return ToolResult(
                False,
                {"error": str(exc), "hint": _path_hint()},
                t("tool.vault_read.bad_path.display"),
            )
        payload: dict[str, Any] = {
            "path": file.path,
            "frontmatter": file.frontmatter.to_dict(),
            "body": file.body,
            "chars": file.total_chars,
            "returned_chars": len(file.body),
        }
        if file.truncated:
            payload["truncated"] = True
            payload["note"] = t(
                "tool.vault_read.truncated",
                total=file.total_chars,
                returned=len(file.body),
            )
        return ToolResult(True, payload, t("tool.vault_read.display", path=file.path))


class VaultWriteTool:
    name = "vault_write"

    def __init__(self, store: VaultStore, *, today: str = "") -> None:
        self._store = store
        self._today = today or date.today().isoformat()
        self.description = t("tool.vault_write.desc")
        self.parameters = {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": _path_hint()},
                "body": {"type": "string", "description": t("tool.vault_write.param.body")},
                "title": {"type": "string", "description": t("tool.vault_write.param.title")},
                "kind": {
                    "type": "string",
                    "description": t("tool.vault_write.param.kind"),
                },
                "relation": {
                    "type": "string",
                    "description": t("tool.vault_write.param.relation"),
                },
                "status": {
                    "type": "string",
                    "description": t("tool.vault_write.param.status"),
                },
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
            },
            "required": ["path", "body"],
        }

    def run(self, args: dict[str, Any]) -> ToolResult:
        raw = str(args.get("path") or "").strip()
        body = str(args.get("body") or "").strip()
        if not raw:
            return ToolResult(
                False,
                {"error": t("tool.common.path_required"), "hint": _path_hint()},
                t("tool.common.path_missing.display"),
            )
        if not body:
            return ToolResult(
                False,
                {"error": t("tool.vault_write.empty_body")},
                t("tool.vault_write.empty_body.display"),
            )

        def _list(key: str) -> tuple[str, ...]:
            value = args.get(key)
            if isinstance(value, list):
                return tuple(str(item).strip() for item in value if str(item).strip())
            if isinstance(value, str) and value.strip():
                return tuple(part.strip() for part in value.split(",") if part.strip())
            return ()

        incoming = Frontmatter(
            title=str(args.get("title") or "").strip(),
            kind=str(args.get("kind") or "").strip(),
            relation=str(args.get("relation") or "").strip(),
            status=str(args.get("status") or "").strip(),
            updated=self._today,
            tags=_list("tags"),
            aliases=_list("aliases"),
        )

        try:
            canonical = self._store.resolve_friendly(raw)
            if "/" not in canonical:
                return ToolResult(
                    False,
                    {
                        "error": t("tool.vault_write.bad_path", path=canonical),
                        "hint": t("tool.vault_write.bad_path.hint"),
                    },
                    t("tool.vault_write.bad_path.display"),
                )
            # 合并元信息、戳 updated、大小写撞名、单文件上限 —— 全在 store 里一份实现，
            # 网页界面走的是同一个方法（ADR-070）
            result = self._store.save_note(canonical, body, incoming, today=self._today)
        except VaultPathError as exc:
            # `str(exc)` 由**档案层**（harness/vault.py，`ui.vault.*`）自己按当前语言取，
            # 这里不复制那份文案
            return ToolResult(
                False,
                {"error": str(exc), "hint": _path_hint()},
                t("tool.common.write_rejected.display"),
            )
        except OSError as exc:
            return ToolResult(
                False,
                {"error": t("tool.common.write_failed", error=exc)},
                t("tool.common.write_failed.display"),
            )

        return ToolResult(
            True,
            {
                "path": result["path"],
                "action": result["action"],
                # 把**回读到的**元信息给模型看：它下一轮就知道 frontmatter 长什么样了
                "frontmatter": result["frontmatter"],
                "body_chars": result["body_chars"],
            },
            t(
                "tool.vault_write.display.created"
                if result["action"] == "created"
                else "tool.vault_write.display.updated",
                path=result["path"],
                chars=result["body_chars"],
            ),
        )


class VaultDeleteTool:
    name = "vault_delete"

    def __init__(self, store: VaultStore) -> None:
        self._store = store
        self.description = t("tool.vault_delete.desc")
        self.parameters = {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": _path_hint()},
                "reason": {
                    "type": "string",
                    "description": t("tool.vault_delete.param.reason"),
                },
            },
            "required": ["path", "reason"],
        }

    def run(self, args: dict[str, Any]) -> ToolResult:
        raw = str(args.get("path") or "").strip()
        reason = str(args.get("reason") or "").strip()
        if not reason:
            return ToolResult(
                False,
                {"error": t("tool.vault_delete.reason_required")},
                t("tool.vault_delete.reason_required.display"),
            )
        try:
            # 删除的唯一实现在 store 里（README 不能删这条规矩在两边一样，ADR-070）
            canonical = self._store.delete_file(raw)
        except VaultPathError as exc:
            hint = _path_hint()
            if "README" in str(exc):
                hint = t("tool.vault_delete.readme_hint")
            return ToolResult(
                False,
                # `str(exc)` 由**档案层**（harness/vault.py，`ui.vault.*`）自己按当前语言取，
            # 这里不复制那份文案
                {"error": str(exc), "hint": hint},
                t("tool.vault_delete.rejected.display"),
            )
        except FileNotFoundError:
            return ToolResult(
                False,
                {"error": t("tool.common.archive_missing", raw=raw)},
                t("tool.vault_delete.missing.display"),
            )
        return ToolResult(
            True,
            {"path": canonical, "reason": reason},
            t("tool.vault_delete.display", path=canonical, reason=reason),
        )


class ReadmeWriteTool:
    name = "readme_write"

    def __init__(self, store: VaultStore) -> None:
        self._store = store
        self.description = t("tool.readme_write.desc")
        self.parameters = {
            "type": "object",
            "properties": {
                "markdown": {
                    "type": "string",
                    "description": t("tool.readme_write.param.markdown"),
                }
            },
            "required": ["markdown"],
        }

    def run(self, args: dict[str, Any]) -> ToolResult:
        markdown = str(args.get("markdown") or "").strip()
        if not markdown:
            return ToolResult(
                False,
                {"error": t("tool.readme_write.empty")},
                t("tool.readme_write.empty.display"),
            )
        # `status: placeholder` 是 harness 给"空骨架"用的标记：带上它，
        # README 会被**永久判为占位、一个字都不注入**（readme_text 返回空）。
        # 模型自己写出这个状态就等于把整个档案层静音了 —— 直接拒绝。
        front, _, _ = parse_frontmatter(markdown)
        if front.status.strip().lower() == PLACEHOLDER_STATUS:
            return ToolResult(
                False,
                {
                    "error": t("tool.readme_write.placeholder"),
                    "hint": t("tool.readme_write.placeholder.hint"),
                },
                t("tool.readme_write.placeholder.display"),
            )
        try:
            canonical = self._store.write_file(README_NAME, markdown + "\n")
        except VaultPathError as exc:
            # `str(exc)` 由**档案层**（harness/vault.py，`ui.vault.*`）自己按当前语言取，
            # 这里不复制那份文案
            return ToolResult(
                False,
                {"error": str(exc)},
                t("tool.common.write_rejected.display"),
            )
        except OSError as exc:
            return ToolResult(
                False,
                {"error": t("tool.common.write_failed", error=exc)},
                t("tool.common.write_failed.display"),
            )

        text, truncated = self._store.readme_text()
        tokens = estimate_tokens(text)
        budget = self._store.readme_token_budget
        payload: dict[str, Any] = {
            "path": canonical,
            "tokens": tokens,
            "budget": budget,
            "over_budget": truncated,
            "chars": len(markdown),
        }
        if truncated:
            # 当场告诉它超了：比等下一轮整理才发现便宜得多
            payload["note"] = t(
                "tool.readme_write.over_budget", tokens=tokens, budget=budget
            )
        return ToolResult(
            True,
            payload,
            t("tool.readme_write.display", tokens=tokens, budget=budget),
        )


def build_dream_tools(
    *,
    store: VaultStore,
    sessions_dir,
    cursors: Mapping[str, int],
    tracker: ReadTracker,
    record_max_events: int = 40,
    record_max_chars: int = 6000,
    max_result_chars: int = 2000,
    today: str = "",
) -> tuple[Any, ...]:
    """按固定顺序组装 dream 的工具集（顺序固定 = tools 数组字节稳定）。"""
    return (
        RecordsListTool(sessions_dir, cursors, max_result_chars=max_result_chars),
        RecordsReadTool(
            sessions_dir,
            cursors,
            tracker,
            max_events=record_max_events,
            max_chars=record_max_chars,
            max_result_chars=max_result_chars,
        ),
        VaultOutlineTool(store, max_result_chars=max_result_chars),
        VaultReadTool(store, max_result_chars=max_result_chars),
        VaultWriteTool(store, today=today),
        VaultDeleteTool(store),
        ReadmeWriteTool(store),
    )
