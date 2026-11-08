"""逐条 memory instructions 的原生 Markdown 存储与 Dream 游标。

有效记忆只存在于 ``data/vault/memory_instruction.md``，形状与 Kimi 原生导出一致：

    # Memory instructions
    ## 1
    第一条正文。
    ## 2
    第二条正文。

文件只保存 id 与正文：没有分类、版本、时间戳、软删除副本或侧车元数据。删除就是从
文件中移除。旧 SQLite 的 memories/memory_ops 不读取、不迁移；SQLite 现在只负责
Dream 的整理游标。`MemoryStore` 仍把两种职责放在同一外观下，是为了让 AgentLoop、
CLI 与 Web 的装配点保持简单，而不是让 Dream 获得写记忆的权限。

文件写入采用“按路径加锁 + 锁内重读 + 原子替换”：Web 的不同请求各有自己的 store，
不能拿构造时的旧快照覆盖另一请求刚写入的内容。读取不需要创建文件；只有第一次真实
新建记忆才会生成 Markdown。
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from .atomic_io import write_text_atomic
from .i18n import t
from .llm_types import estimate_tokens
from .memory_limit import limit_for

MEMORY_HEADER = "# Memory instructions"
_HEADING = re.compile(r"^## ([1-9][0-9]*)[ \t]*$")
_CURSOR_DDL = """
CREATE TABLE IF NOT EXISTS dream_cursor (
    session_id  TEXT PRIMARY KEY,
    last_seq    INTEGER NOT NULL DEFAULT 0,
    recorded_at TEXT NOT NULL,
    run_id      TEXT
)
"""

_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _path_lock(path: Path) -> threading.RLock:
    try:
        key = str(path.resolve()).casefold()
    except (OSError, RuntimeError):
        key = str(path.absolute()).casefold()
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _normalize(text: str) -> str:
    """用于查重：忽略大小写与所有空白。"""
    return "".join(text.split()).casefold()


@dataclass(frozen=True)
class MemoryItem:
    id: int
    content: str


class MemoryFormatError(ValueError):
    """原生 memory_instruction.md 不是能无损理解的格式。"""


def parse_memory_instructions(text: str) -> list[MemoryItem]:
    """解析原生 Markdown；坏结构直接报错，绝不静默丢条目。

    允许 UTF-8 BOM、文件末尾空行和条目内部的空行。标题必须是 `## <正整数>`；
    文件头之前或标题之间的非空杂项都会拒绝，因为继续读就等于接受一份已经失真的档案。
    """
    lines = text.lstrip("\ufeff").splitlines()
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines) or lines[index].strip() != MEMORY_HEADER:
        raise MemoryFormatError(t("ui.memory.format_header", header=MEMORY_HEADER))
    index += 1

    items: list[MemoryItem] = []
    seen: set[int] = set()
    current_id: int | None = None
    body: list[str] = []

    def flush() -> None:
        nonlocal current_id, body
        if current_id is None:
            return
        content = "\n".join(body).strip()
        if not content:
            raise MemoryFormatError(t("ui.memory.format_empty", id=current_id))
        if current_id in seen:
            raise MemoryFormatError(t("ui.memory.format_duplicate_id", id=current_id))
        seen.add(current_id)
        items.append(MemoryItem(id=current_id, content=content))
        current_id = None
        body = []

    for line in lines[index:]:
        match = _HEADING.fullmatch(line)
        if match:
            flush()
            current_id = int(match.group(1))
            body = []
            continue
        if current_id is None:
            if line.strip():
                raise MemoryFormatError(t("ui.memory.format_junk", line=line.strip()))
            continue
        body.append(line)
    flush()
    return items


def read_memory_instructions(path: Path) -> list[MemoryItem]:
    """只读加载；文件不存在就是空记忆，不为“看一眼”创建文件。"""
    path = Path(path)
    if not path.is_file():
        return []
    return parse_memory_instructions(path.read_text(encoding="utf-8-sig"))


def render_memory_instructions(items: Iterable[MemoryItem]) -> str:
    """按传入顺序渲染；新建追加、编辑原位、删除收缩，id 不必连续。"""
    blocks = [MEMORY_HEADER]
    blocks.extend(f"## {item.id}\n\n{item.content.strip()}" for item in items)
    return "\n\n".join(blocks).rstrip() + "\n"


class MemoryStore:
    def __init__(
        self,
        *,
        instruction_path: Path | str,
        cursor_db_path: Path | str | None = None,
        max_items: int = 200,
        max_chars: int = 400,
    ) -> None:
        self.instruction_path = Path(instruction_path)
        self._in_memory = str(instruction_path) == ":memory:"
        self._items: list[MemoryItem] = []
        self.max_items = max_items
        self.max_chars = max_chars

        # 独立调用（测试、prompt_view）不需要持久游标；生产由 open_memory 传入 memory.db。
        self.db_path = Path(cursor_db_path or ":memory:")
        if self.db_path != Path(":memory:"):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=10000")
        # 不在构造时切换日志模式：另一个写入者已持事务时 PRAGMA journal_mode=WAL
        # 会直接 database is locked。游标写事务很短，BEGIN IMMEDIATE 已足够互斥。
        self._conn.execute(_CURSOR_DDL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # 原生 Markdown 读写
    # ------------------------------------------------------------------

    def _read_items(self) -> list[MemoryItem]:
        if self._in_memory:
            return list(self._items)
        return read_memory_instructions(self.instruction_path)

    def _write_items(self, items: list[MemoryItem]) -> None:
        if self._in_memory:
            self._items = list(items)
            return
        write_text_atomic(self.instruction_path, render_memory_instructions(items))

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        """串行化同文件的读—改—写，包括另一个进程里的 CLI/Web。"""
        with _path_lock(self.instruction_path):
            with self._conn:
                # SQLite 只作跨进程互斥锁；schema 仍只有 dream_cursor，不存记忆正文。
                self._conn.execute("BEGIN IMMEDIATE")
                yield

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def active_items(self) -> list[MemoryItem]:
        return sorted(self._read_items(), key=lambda item: item.id)

    def get(self, memory_id: int) -> MemoryItem | None:
        try:
            wanted = int(memory_id)
        except (TypeError, ValueError):
            return None
        return next((item for item in self._read_items() if item.id == wanted), None)

    def find_active_by_content(
        self, content: str, *, exclude_id: int | None = None, exclude_ids: Iterable[int] = ()
    ) -> MemoryItem | None:
        target = _normalize(content)
        skip = set(exclude_ids)
        if exclude_id is not None:
            skip.add(exclude_id)
        for item in self.active_items():
            if item.id not in skip and _normalize(item.content) == target:
                return item
        return None

    def stats(self) -> dict[str, int]:
        return {"active": len(self._read_items())}

    # ------------------------------------------------------------------
    # Dream 游标（SQLite 的唯一剩余职责）
    # ------------------------------------------------------------------

    def cursor_map(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT session_id, last_seq FROM dream_cursor").fetchall()
        return {str(row["session_id"]): int(row["last_seq"]) for row in rows}

    def clear_cursors(self) -> int:
        with self._conn:
            cursor = self._conn.execute("DELETE FROM dream_cursor")
            return int(cursor.rowcount or 0)

    def advance_cursor(self, session_id: str, last_seq: int, run_id: str = "") -> None:
        """推进游标（只增不减），避免未读区间被永久跳过。"""
        session_id = str(session_id or "").strip()
        if not session_id:
            return
        seq = max(0, int(last_seq))
        with self._conn:
            self._conn.execute(
                "INSERT INTO dream_cursor (session_id, last_seq, recorded_at, run_id)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(session_id) DO UPDATE SET"
                "   last_seq = MAX(dream_cursor.last_seq, excluded.last_seq),"
                "   recorded_at = excluded.recorded_at,"
                "   run_id = excluded.run_id",
                (session_id, seq, _now(), run_id),
            )

    def rewind_cursor(self, session_id: str, last_seq: int, run_id: str = "") -> None:
        """仅在日志已机械确认变短时，为重读保存新的真实进度。"""
        session_id = str(session_id or "").strip()
        if not session_id:
            return
        seq = max(0, int(last_seq))
        with self._conn:
            self._conn.execute(
                "INSERT INTO dream_cursor (session_id, last_seq, recorded_at, run_id)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(session_id) DO UPDATE SET"
                "   last_seq = excluded.last_seq,"
                "   recorded_at = excluded.recorded_at,"
                "   run_id = excluded.run_id",
                (session_id, seq, _now(), run_id),
            )

    def inherit_cursor(self, src_session_id: str, dst_session_id: str, cap_seq: int) -> int:
        src = max(0, int(self.cursor_map().get(str(src_session_id), 0)))
        inherited = min(src, max(0, int(cap_seq)))
        if inherited > 0:
            self.advance_cursor(str(dst_session_id), inherited)
        return inherited

    # ------------------------------------------------------------------
    # 注入
    # ------------------------------------------------------------------

    def injection_plan(self, *, max_tokens: int) -> tuple[list[MemoryItem], int]:
        items = self.active_items()
        cost = estimate_tokens("\n".join(self.format_line(item) for item in items))
        if cost <= max_tokens:
            return items, 0
        chosen: list[MemoryItem] = []
        used = 0
        for item in sorted(items, key=lambda entry: entry.id, reverse=True):
            price = estimate_tokens(self.format_line(item)) + 1
            if chosen and used + price > max_tokens:
                break
            chosen.append(item)
            used += price
        chosen.sort(key=lambda item: item.id)
        return chosen, len(items) - len(chosen)

    def injection_view(self, *, max_tokens: int) -> tuple[str, int, int]:
        """同一份文件快照生成正文、省略数与总数，避免并发写入时口径分家。"""
        items, omitted = self.injection_plan(max_tokens=max_tokens)
        total = len(items) + omitted
        header = t("ui.memory.header", used=total, limit=self.max_items)
        if not items:
            return f"{header}\n{t('ui.memory.empty')}", omitted, total
        lines = [self.format_line(item) for item in items]
        if omitted:
            lines.append(t("ui.memory.omitted", omitted=omitted))
        return header + "\n" + "\n".join(lines), omitted, total

    def render_for_prompt(self, *, max_tokens: int = 6000) -> str:
        return self.injection_view(max_tokens=max_tokens)[0]

    @staticmethod
    def format_line(item: MemoryItem) -> str:
        return f"[mem:{item.id}] {item.content}"

    # ------------------------------------------------------------------
    # 写入：锁内重读，防止请求实例互相覆盖
    # ------------------------------------------------------------------

    def create(self, content: str) -> dict[str, Any]:
        content = (content or "").strip()
        error = self._validate_content(content)
        if error:
            return {"ok": False, "error": error}

        with self._write_transaction():
            items = self._read_items()
            duplicate = next(
                (item for item in items if _normalize(item.content) == _normalize(content)),
                None,
            )
            if duplicate is not None:
                return {
                    "ok": False,
                    "error": t("ui.memory.duplicate"),
                    "existing_id": duplicate.id,
                    "hint": t("ui.memory.duplicate_hint"),
                }
            active_count = len(items)
            if active_count >= self.max_items:
                return {
                    "ok": False,
                    "error": t("ui.memory.too_many", count=active_count, limit=self.max_items),
                    "active_count": active_count,
                    "max_items": self.max_items,
                    "hint": t("ui.memory.too_many_hint"),
                }
            # 原生存储没有 next-id 元数据。用毫秒时间戳作下界，避免删除最大 id 后
            # 立刻复用；同一毫秒内碰撞则递增。id 始终低于 JavaScript 的安全整数上限。
            memory_id = max(
                int(time.time() * 1000),
                max((item.id for item in items), default=0) + 1,
            )
            while any(item.id == memory_id for item in items):
                memory_id += 1
            items.append(MemoryItem(id=memory_id, content=content))
            self._write_items(items)
        return {
            "ok": True,
            "id": memory_id,
            "content": content,
            "active_count": active_count + 1,
            "max_items": self.max_items,
            "note": t("ui.memory.created"),
        }

    def edit(self, memory_id: int, content: str | None = None) -> dict[str, Any]:
        if content is None:
            return {"ok": False, "error": t("ui.memory.need_a_field")}
        new_content = (content or "").strip()
        error = self._validate_content(new_content)
        if error:
            return {"ok": False, "error": error}

        with self._write_transaction():
            items = self._read_items()
            index = next((i for i, item in enumerate(items) if item.id == memory_id), None)
            if index is None:
                return {
                    "ok": False,
                    "error": t("ui.memory.no_such_id", id=memory_id),
                    "hint": t("ui.memory.edit_missing_hint"),
                }
            current = items[index]
            duplicate = next(
                (
                    item
                    for item in items
                    if item.id != memory_id and _normalize(item.content) == _normalize(new_content)
                ),
                None,
            )
            if duplicate is not None:
                return {
                    "ok": False,
                    "error": t("ui.memory.edit_duplicate"),
                    "existing_id": duplicate.id,
                    "hint": t("ui.memory.edit_duplicate_hint"),
                }
            if current.content == new_content:
                return {
                    "ok": True,
                    "id": memory_id,
                    "content": current.content,
                    "note": t("ui.memory.unchanged"),
                }
            items[index] = MemoryItem(id=memory_id, content=new_content)
            self._write_items(items)
        return {
            "ok": True,
            "id": memory_id,
            "content": new_content,
            "note": t("ui.memory.updated"),
        }

    def delete(self, memory_id: int, reason: str | None = None) -> dict[str, Any]:
        with self._write_transaction():
            items = self._read_items()
            current = next((item for item in items if item.id == memory_id), None)
            if current is None:
                return {"ok": False, "error": t("ui.memory.no_such_id_deleted", id=memory_id)}
            self._write_items([item for item in items if item.id != memory_id])
        payload: dict[str, Any] = {
            "ok": True,
            "id": memory_id,
            "forgot": current.content,
            "note": t("ui.memory.deleted"),
        }
        if reason:
            payload["reason"] = reason
        return payload

    def _validate_content(self, content: str) -> str | None:
        if not content:
            return t("ui.memory.content_empty")
        if len(content) > self.max_chars:
            return t("ui.memory.content_too_long", length=len(content), limit=self.max_chars)
        if any(_HEADING.fullmatch(line) for line in content.splitlines()):
            # 原生格式没有转义语法；这种行重读时会变成另一条记忆，必须在写入前拒绝。
            return t("ui.memory.content_reserved_heading")
        return None

def open_memory(settings: Any) -> MemoryStore:
    """按当前生效的上限打开原生记忆；`memory.db` 只承载 Dream 游标。"""
    return MemoryStore(
        instruction_path=settings.memory_instruction_path,
        cursor_db_path=settings.memory_db_path,
        max_items=limit_for(settings.data_dir, settings.memory_limit),
        max_chars=settings.max_memory_chars,
    )
