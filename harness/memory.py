"""长期记忆存储（SQLite）。

设计决策（详见 docs/01-decisions.md）：
  * **删除是软删除**：模型视角是"忘了"，但 status='deleted' 保留了原文。
    误删可回溯。
  * **注入顺序默认按 id 升序**：保持 prompt 前缀稳定以命中 prompt cache。
  * **记忆是全量注入的，且有条数硬上限**（默认 50，可在设置里改，ADR-057）：
    上限就是"每轮固定要花多少 token"的那道闸。满了以后 `mem_create` 直接失败，
    由模型改用 `mem_edit` 合并 —— 不悄悄丢掉最旧的那条。
  * memory_ops 表记录每一次增删改，供调试与审计。
  * **这张表只由对话模型写**（ADR-053）：后台的 dream 巩固产出的是背景档案
    （`data/vault/`），它**不再改写用户的记忆条目** —— 所以 M4 那套
    "合并 / 改写 / 提炼洞察 / 快照回滚"的机制整体退役了（ADR-059）。

**dream 游标为什么在这张库里**：`dream_cursor` 记录"每个会话整理到第几条"。
它本来也可以放一个 JSON 文件，但反正要在同一次 schema 迁移里把 M4 留下的死列
（`aliases` / `sources` / `dreamed_at` / `dream_run`）删掉，就顺手放这儿了。
**同库不等于同所有权**：档案内容在文件里，dream 只有这一个计数器在这里。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .llm_types import estimate_tokens
from .i18n import t
from .memory_limit import limit_for

# 建表/迁移的互斥锁：Web 端每个请求各自开连接，多线程同时 ALTER TABLE 会撞车
_MIGRATION_LOCK = threading.Lock()

_MEMORIES_DDL = """
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content     TEXT    NOT NULL,
    category    TEXT    NOT NULL DEFAULT 'general',
    status      TEXT    NOT NULL DEFAULT 'active',
    revision    INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    deleted_at  TEXT,
    session_id  TEXT,
    turn        INTEGER
)"""

_MEMORY_OPS_DDL = """
CREATE TABLE IF NOT EXISTS memory_ops (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id  INTEGER NOT NULL,
    op         TEXT    NOT NULL,
    before     TEXT,
    after      TEXT,
    session_id TEXT,
    turn       INTEGER,
    at         TEXT    NOT NULL
)"""

# dream 的整理进度：每个会话整理到第几条（含）。只增不减。
_DREAM_CURSOR_DDL = """
CREATE TABLE IF NOT EXISTS dream_cursor (
    session_id  TEXT PRIMARY KEY,
    last_seq    INTEGER NOT NULL DEFAULT 0,
    recorded_at TEXT    NOT NULL,
    run_id      TEXT
)"""

_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);
CREATE INDEX IF NOT EXISTS idx_memory_ops_memory ON memory_ops(memory_id);
"""

# 注意：`executescript` 靠**分号**切语句，用换行拼会得到一个语法错误 ——
# 全新库那条路径（表还不存在）正好走这里，而且只在全新安装时才会踩到。
SCHEMA = ";\n".join((_MEMORIES_DDL, _MEMORY_OPS_DDL, _DREAM_CURSOR_DDL, _INDEX_DDL)) + ";"

# M1~M6 期间陆续加进 memories 的基础列。老库缺了就补，补完再谈迁移。
_LEGACY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("deleted_at", "TEXT"),
    ("session_id", "TEXT"),
    ("turn", "INTEGER"),
    ("revision", "INTEGER NOT NULL DEFAULT 1"),
)

# M4 留下的**死列**：dream 改成只写 vault 之后，它们没有任何读取方
#   aliases     —— 随超量检索机制一起作废（ADR-058）
#   sources     —— 只有 dream 的合并/提炼在写，那两个工具已退役（ADR-059）
#   dreamed_at / dream_run —— 旧的 dream 游标，被 dream_cursor 取代
_DEAD_COLUMNS: tuple[str, ...] = ("aliases", "sources", "dreamed_at", "dream_run")

# 重建表时保留的列（顺序固定，便于复制）
_KEEP_COLUMNS: tuple[str, ...] = (
    "id",
    "content",
    "category",
    "status",
    "revision",
    "created_at",
    "updated_at",
    "deleted_at",
    "session_id",
    "turn",
)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _normalize(text: str) -> str:
    """用于查重：忽略大小写与所有空白。"""
    return "".join(text.split()).casefold()


@dataclass
class MemoryItem:
    id: int
    content: str
    category: str
    revision: int
    created_at: str
    updated_at: str
    status: str = "active"
    deleted_at: str | None = None

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "MemoryItem":
        return cls(
            id=int(row["id"]),
            content=str(row["content"]),
            category=str(row["category"]),
            revision=int(row["revision"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            status=str(row["status"]),
            deleted_at=row["deleted_at"],
        )


class MemoryStore:
    def __init__(
        self,
        db_path: Path,
        *,
        max_items: int = 200,
        max_chars: int = 400,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_items = max_items
        self.max_chars = max_chars
        self._session_id: str | None = None
        self._turn: int | None = None
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # 建表与迁移（ADR-059：一次做完，不留第二次改 schema）
    # ------------------------------------------------------------------

    def _table_names(self) -> set[str]:
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        return {str(row["name"]) for row in rows}

    def _columns(self, table: str) -> set[str]:
        return {
            str(row["name"]) for row in self._conn.execute(f"PRAGMA table_info({table})")
        }

    def _top_up(self, table: str, columns: set[str]) -> None:
        """把老库缺的基础列补上（sqlite 没有 ADD COLUMN IF NOT EXISTS）。"""
        for column, declaration in _LEGACY_COLUMNS:
            if column not in columns:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
                )
                columns.add(column)

    def _migrate(self) -> None:
        """建表 + 一次性迁移。

        这次迁移做三件事，都是"dream 不再改写记忆条目"的直接后果：

        1. 补上老库缺的基础列；
        2. **删掉 M4 留下的死列** `aliases` / `sources` / `dreamed_at` / `dream_run`；
        3. **删掉旧的 `dream_runs` 表**，新建 `dream_cursor`。

        第 3 条值得解释：旧表里每次运行都存着一份**记忆库整体快照**，为的是
        `--dream-undo`。但现在 dream 不碰记忆条目了 —— 拿一份旧快照去还原，
        会把用户在这之后**自己**记下的东西一起抹掉。那不是回滚 dream，是回滚用户。
        所以连表一起删；那几次运行留下的审计痕迹仍在 `memory_ops` 里。

        删列用**重建表**而不是 `ALTER TABLE ... DROP COLUMN`：后者对索引和
        SQLite 版本有要求，而重建（建新表 → 复制 → 换名）在任何版本上都成立。
        """
        with _MIGRATION_LOCK:
            self._repair_interrupted_migration()
            if "memories" not in self._table_names():
                self._conn.executescript(SCHEMA)
                return

            columns = self._columns("memories")
            self._top_up("memories", columns)

            dead = [name for name in _DEAD_COLUMNS if name in columns]
            if dead:
                keep = ", ".join(name for name in _KEEP_COLUMNS if name in columns)
                self._conn.execute("DROP INDEX IF EXISTS idx_memories_status")
                self._conn.execute("ALTER TABLE memories RENAME TO memories_legacy")
                self._conn.execute(_MEMORIES_DDL)
                self._conn.execute(
                    f"INSERT INTO memories ({keep}) SELECT {keep} FROM memories_legacy"
                )
                self._conn.execute("DROP TABLE memories_legacy")
                self._conn.commit()

            self._conn.execute("DROP TABLE IF EXISTS dream_runs")
            self._conn.executescript(_INDEX_DDL)
            self._conn.executescript(_DREAM_CURSOR_DDL)
            self._conn.commit()

    def _repair_interrupted_migration(self) -> None:
        """收拾上一次迁移半路失败留下的 `memories_legacy`。

        这类"把话说完"的逻辑必须有：半截迁移会让用户拿到一个**看起来空了**的
        记忆库（`memories` 建好了但数据还在旧表里）—— 那比直接报错更糟，
        因为他会以为记忆被清空了。
        """
        if "memories_legacy" not in self._table_names():
            return
        if "memories" not in self._table_names():
            self._conn.execute("ALTER TABLE memories_legacy RENAME TO memories")
            self._conn.commit()
            return
        self._top_up("memories_legacy", self._columns("memories_legacy"))
        keep = ", ".join(_KEEP_COLUMNS)
        self._conn.execute(
            f"INSERT OR IGNORE INTO memories ({keep}) SELECT {keep} FROM memories_legacy"
        )
        self._conn.execute("DROP TABLE memories_legacy")
        self._conn.commit()

    # ------------------------------------------------------------------
    # 回合上下文（写进 memory_ops，便于审计"哪轮对话写了什么记忆"）
    # ------------------------------------------------------------------

    def set_context(self, session_id: str | None, turn: int | None) -> None:
        self._session_id = session_id
        self._turn = turn

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def active_items(self) -> list[MemoryItem]:
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE status = 'active' ORDER BY id ASC"
        ).fetchall()
        return [MemoryItem.from_row(row) for row in rows]

    def all_items(self) -> list[MemoryItem]:
        rows = self._conn.execute("SELECT * FROM memories ORDER BY id ASC").fetchall()
        return [MemoryItem.from_row(row) for row in rows]

    def get(self, memory_id: int, *, include_deleted: bool = False) -> MemoryItem | None:
        row = self._conn.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            return None
        item = MemoryItem.from_row(row)
        if item.status != "active" and not include_deleted:
            return None
        return item

    def find_active_by_content(
        self, content: str, *, exclude_id: int | None = None, exclude_ids: Iterable[int] = ()
    ) -> MemoryItem | None:
        target = _normalize(content)
        skip = set(exclude_ids)
        if exclude_id is not None:
            skip.add(exclude_id)
        for item in self.active_items():
            if item.id in skip:
                continue
            if _normalize(item.content) == target:
                return item
        return None

    def stats(self) -> dict[str, int]:
        active = self._conn.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE status = 'active'"
        ).fetchone()["n"]
        deleted = self._conn.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE status = 'deleted'"
        ).fetchone()["n"]
        ops = self._conn.execute("SELECT COUNT(*) AS n FROM memory_ops").fetchone()["n"]
        return {
            "active": int(active),
            "deleted": int(deleted),
            "ops": int(ops),
        }

    # ------------------------------------------------------------------
    # dream 游标：每个会话"整理到第几条"
    #
    # 它放在这张库里只是因为要在同一次 schema 迁移里顺手建（ADR-059），
    # **不代表 dream 对记忆条目有任何写权限**（ADR-053）。
    # ------------------------------------------------------------------

    def cursor_map(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT session_id, last_seq FROM dream_cursor"
        ).fetchall()
        return {str(row["session_id"]): int(row["last_seq"]) for row in rows}

    def clear_cursors(self) -> int:
        """清掉**全部**整理进度，返回清掉了几条。

        这是"格式化 dream 层"的一部分（ADR-072）：档案被清空之后，进度必须一起没 ——
        否则会出现"档案是空的、进度却说整理过了"这种自相矛盾的状态，
        于是那些会话**再也不会**被整理，内容等于永久消失。

        它比 `rewind_cursor` 更彻底，但因为同样是"让已整理的内容重新被整理"，
        所以风险方向是**多花钱**而不是丢数据 —— 这也是它只用在格式化上的原因。
        """
        with self._conn:
            cursor = self._conn.execute("DELETE FROM dream_cursor")
            return int(cursor.rowcount or 0)

    def advance_cursor(self, session_id: str, last_seq: int, run_id: str = "") -> None:
        """把某个会话的游标推进到 `last_seq`（**只增不减**）。

        "只增不减"是硬的：游标值意味着"这个会话的前 N 条已经进了档案"，
        把它调小只会让同样的内容被整理两遍（花两次钱），调大则会**永久丢掉**中间没读的记录。
        所以写入用 `MAX()`，任何调用方都不可能让它回退。
        """
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
        """把游标**退**到某个值。

        正常整理绝不能退游标（ADR-065 取消了 dream 的回滚能力）。唯一的自动例外是
        DreamRunner 已机械检测到日志变短：旧高水位指向的已不是这份文件，成功从头
        重读后必须先清掉旧值，才能保存新日志的真实进度。其余场景只供人工纠正，
        不能用来撤销一次整理。
        """
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

    def inherit_cursor(
        self, src_session_id: str, dst_session_id: str, cap_seq: int
    ) -> int:
        """把 src 的整理游标继承给 dst，但不超过 `cap_seq`（分叉会话用）。

        分叉出的新会话是原会话的一个**前缀**（事件 seq 原样保留）。原会话里
        "已经整理到 seq M"意味着"seq ≤ M 的事件都进过背景档案"；新会话复制过来的
        正是这些事件，所以新游标 = `min(M, cap_seq)` —— 复制来的历史里**已经整理过
        的部分**不该再整理一遍（否则档案会出现近似重复），没整理过的尾巴照常待整理。

        返回写进 dst 的游标值；0 = 没有可继承的进度，**不写任何行**（缺行 = 游标 0，
        与"一条都没整理过"是同一个语义，所以没必要写个 0 进去）。
        """
        src = max(0, int(self.cursor_map().get(str(src_session_id), 0)))
        inherited = min(src, max(0, int(cap_seq)))
        if inherited > 0:
            self.advance_cursor(str(dst_session_id), inherited)
        return inherited

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------

    def injection_plan(self, *, max_tokens: int) -> tuple[list[MemoryItem], int]:
        """这一轮实际会注入哪些条目、有几条因预算看不到。

        抽出来是为了让**界面**也能说同一句话：`--memories` 与记忆页要能告诉用户
        "有 N 条因为太长没进上下文"，否则那条信息只存在于模型看到的提示词里。
        """
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

    def render_for_prompt(self, *, max_tokens: int = 6000) -> str:
        """渲染注入上下文的记忆块。

        **全量注入**（ADR-058）：条数上限（默认 50）就是"条目数"这道闸，
        所以不再按相关性挑选 —— 每条都在，模型看到的就是全部。

        唯一会少的情形是"条目总量超过 token 预算"。那时**必须说出来**：
        提示词里同时写着"上限 50 条"和"已用 50/50"，如果不解释为什么只看到一部分，
        模型会以为自己记的东西丢了。

        表头的条数**从同一次 `injection_plan()` 推出来**（`len(items) + omitted`），
        而不是再查一次库：Web 是多线程的，两次查询之间可能有别的线程写了一条，
        那样表头就会和正文对不上 —— 同一个数字两套口径，正是要避免的东西。
        """
        items, omitted = self.injection_plan(max_tokens=max_tokens)
        total = len(items) + omitted
        # 表头这几行是**模型读到的框架文字**，所以要跟着界面语言走 ——
        # 而且在**用到它的这一刻**取（语言是运行期可切的，import 时算好就换不动了）。
        header = t("ui.memory.header", used=total, limit=self.max_items)
        if not items:
            return f"{header}\n{t('ui.memory.empty')}"
        lines = [self.format_line(item) for item in items]
        if omitted:
            lines.append(t("ui.memory.omitted", omitted=omitted))
        return header + "\n" + "\n".join(lines)

    @staticmethod
    def format_line(item: MemoryItem) -> str:
        return f"[mem:{item.id}] ({item.category}) {item.content}"

    # ------------------------------------------------------------------
    # 写入（返回给模型的都是 JSON 友好的 dict）
    # ------------------------------------------------------------------

    def create(
        self,
        content: str,
        category: str | None = None,
        *,
        op: str = "create",
    ) -> dict[str, Any]:
        content = (content or "").strip()
        category = (category or "general").strip() or "general"

        error = self._validate_content(content)
        if error:
            return {"ok": False, "error": error}

        now = _now()
        with self._conn:
            # Web 的每个请求各有连接；`with` 本身只会延迟到第一条写入才开事务，
            # 查重和计数仍会留出竞态窗口。先抢写锁，让检查与插入落在同一事务里。
            self._conn.execute("BEGIN IMMEDIATE")
            duplicate = self.find_active_by_content(content)
            if duplicate is not None:
                return {
                    "ok": False,
                    "error": t("ui.memory.duplicate"),
                    "existing_id": duplicate.id,
                    "hint": t("ui.memory.duplicate_hint"),
                }

            active_count = self.stats()["active"]
            if active_count >= self.max_items:
                # **满了要满得响**（ADR-057）：不悄悄丢掉最旧的那条，而是直接失败，
                # 并把"现在几条 / 上限几条 / 你还能做什么"一次说清 ——
                # 模型看到这句话才知道该去 mem_edit 合并，而不是反复重试。
                return {
                    "ok": False,
                    "error": t(
                        "ui.memory.too_many",
                        count=active_count,
                        limit=self.max_items,
                    ),
                    "active_count": active_count,
                    "max_items": self.max_items,
                    "hint": t("ui.memory.too_many_hint"),
                }

            cursor = self._conn.execute(
                "INSERT INTO memories (content, category, status, revision, created_at,"
                " updated_at, session_id, turn)"
                " VALUES (?, ?, 'active', 1, ?, ?, ?, ?)",
                (
                    content,
                    category,
                    now,
                    now,
                    self._session_id,
                    self._turn,
                ),
            )
            memory_id = int(cursor.lastrowid or 0)
            self._log_op(memory_id, op, None, content)
        return {
            "ok": True,
            "id": memory_id,
            "content": content,
            "category": category,
            "active_count": active_count + 1,
            "max_items": self.max_items,
            "note": t("ui.memory.created"),
        }

    def edit(
        self,
        memory_id: int,
        content: str | None = None,
        category: str | None = None,
    ) -> dict[str, Any]:
        with self._conn:
            # 编辑也能把另一条内容改成重复值，因此与 create 共用同一条串行化边界。
            self._conn.execute("BEGIN IMMEDIATE")
            item = self.get(memory_id)
            if item is None:
                return {
                    "ok": False,
                    "error": t("ui.memory.no_such_id", id=memory_id),
                    "hint": t("ui.memory.edit_missing_hint"),
                }
            if content is None and category is None:
                return {"ok": False, "error": t("ui.memory.need_a_field")}

            new_content = item.content if content is None else (content or "").strip()
            new_category = (
                item.category
                if category is None
                else ((category or "").strip() or item.category)
            )

            if content is not None:
                error = self._validate_content(new_content)
                if error:
                    return {"ok": False, "error": error}
                duplicate = self.find_active_by_content(new_content, exclude_id=memory_id)
                if duplicate is not None:
                    return {
                        "ok": False,
                        "error": t("ui.memory.edit_duplicate"),
                        "existing_id": duplicate.id,
                        "hint": t("ui.memory.edit_duplicate_hint"),
                    }

            if new_content == item.content and new_category == item.category:
                return {
                    "ok": True,
                    "id": memory_id,
                    "content": item.content,
                    "revision": item.revision,
                    "note": t("ui.memory.unchanged"),
                }

            now = _now()
            self._conn.execute(
                "UPDATE memories SET content = ?, category = ?, revision = revision + 1,"
                " updated_at = ?, session_id = ?, turn = ? WHERE id = ?",
                (new_content, new_category, now, self._session_id, self._turn, memory_id),
            )
            self._log_op(memory_id, "edit", item.content, new_content)
            return {
                "ok": True,
                "id": memory_id,
                "content": new_content,
                "category": new_category,
                "revision": item.revision + 1,
                "note": t("ui.memory.updated"),
            }

    def delete(
        self, memory_id: int, reason: str | None = None, *, op: str = "delete"
    ) -> dict[str, Any]:
        item = self.get(memory_id)
        if item is None:
            return {
                "ok": False,
                "error": t("ui.memory.no_such_id_deleted", id=memory_id),
            }
        now = _now()
        with self._conn:
            self._conn.execute(
                "UPDATE memories SET status = 'deleted', deleted_at = ?, session_id = ?,"
                " turn = ? WHERE id = ?",
                (now, self._session_id, self._turn, memory_id),
            )
            self._log_op(memory_id, op, item.content, reason)
        payload: dict[str, Any] = {
            "ok": True,
            "id": memory_id,
            "forgot": item.content,
            "note": t("ui.memory.deleted"),
        }
        if reason:
            payload["reason"] = reason
        return payload

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _validate_content(self, content: str) -> str | None:
        if not content:
            return t("ui.memory.content_empty")
        if len(content) > self.max_chars:
            return t(
                "ui.memory.content_too_long",
                length=len(content),
                limit=self.max_chars,
            )
        return None

    def _log_op(
        self, memory_id: int, op: str, before: str | None, after: str | None
    ) -> None:
        self._conn.execute(
            "INSERT INTO memory_ops (memory_id, op, before, after, session_id, turn, at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (memory_id, op, before, after, self._session_id, self._turn, _now()),
        )

    def ops_for(self, memory_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM memory_ops WHERE memory_id = ? ORDER BY id ASC", (memory_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def dump_json(self) -> str:
        return json.dumps(
            [
                {
                    "id": item.id,
                    "content": item.content,
                    "category": item.category,
                    "status": item.status,
                    "revision": item.revision,
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                }
                for item in self.all_items()
            ],
            ensure_ascii=False,
            indent=2,
        )


# --------------------------------------------------------------------------
# 唯一的构造入口
# --------------------------------------------------------------------------


def open_memory(settings: Any) -> MemoryStore:
    """按**当前生效**的条数上限打开记忆库。

    每个构造点都必须经过这里（ADR-057）。理由：上限是运行期可改的
    （`data/memory.json`，设置界面里改），散落各处自己拼 `max_items=...`
    迟会有一个地方忘了读新值 —— 那正是"设置里改了、界面也显示改了、
    行为却没变"这类 bug 的来源。

    参数用的是鸭子类型（只要 `memory_db_path` / `data_dir` / `memory_limit` /
    `max_memory_chars` 四个属性），这样存储层不需要反过来依赖 config 层。
    """
    return MemoryStore(
        settings.memory_db_path,
        max_items=limit_for(settings.data_dir, settings.memory_limit),
        max_chars=settings.max_memory_chars,
    )
