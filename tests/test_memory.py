"""记忆存储测试：增删改查、查重、上限、软删除、持久化、注入渲染。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from harness.memory import _DEAD_COLUMNS, MemoryStore


class MemoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.store = MemoryStore(self.tmp / "memory.db", max_items=5, max_chars=40)
        self.addCleanup(self.store.close)

    # -- 基本流程 ----------------------------------------------------

    def test_create_read_edit_delete(self) -> None:
        created = self.store.create("用户喜欢美式咖啡", "偏好")
        self.assertTrue(created["ok"])
        memory_id = created["id"]
        self.assertEqual(created["category"], "偏好")

        item = self.store.get(memory_id)
        assert item is not None
        self.assertEqual(item.content, "用户喜欢美式咖啡")
        self.assertEqual(item.revision, 1)

        edited = self.store.edit(memory_id, content="用户更喜欢拿铁")
        self.assertTrue(edited["ok"])
        self.assertEqual(edited["revision"], 2)
        item = self.store.get(memory_id)
        assert item is not None
        self.assertEqual(item.content, "用户更喜欢拿铁")

        deleted = self.store.delete(memory_id, reason="用户要求忘记")
        self.assertTrue(deleted["ok"])
        self.assertEqual(deleted["forgot"], "用户更喜欢拿铁")
        self.assertIsNone(self.store.get(memory_id))
        self.assertEqual(self.store.stats()["active"], 0)

    def test_delete_is_soft_and_audited(self) -> None:
        memory_id = self.store.create("用户在做 O3 zone 项目")["id"]
        self.store.delete(memory_id)

        # 软删除：对外不可见，但原文仍在，且操作可审计
        self.assertEqual(self.store.active_items(), [])
        all_items = self.store.all_items()
        self.assertEqual(len(all_items), 1)
        self.assertEqual(all_items[0].status, "deleted")
        ops = self.store.ops_for(memory_id)
        self.assertEqual([op["op"] for op in ops], ["create", "delete"])

    def test_persists_across_reopen(self) -> None:
        self.store.create("用户的时间很有限", "身份")
        self.store.close()

        reopened = MemoryStore(self.tmp / "memory.db", max_items=5, max_chars=40)
        try:
            items = reopened.active_items()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].content, "用户的时间很有限")
        finally:
            reopened.close()

    # -- 校验与查重 --------------------------------------------------

    def test_rejects_empty_and_too_long(self) -> None:
        self.assertFalse(self.store.create("")["ok"])
        self.assertFalse(self.store.create("   ")["ok"])
        long_content = "很" * 41
        result = self.store.create(long_content)
        self.assertFalse(result["ok"])
        self.assertIn("过长", result["error"])
        self.assertEqual(self.store.stats()["active"], 0)

    def test_rejects_duplicate(self) -> None:
        first = self.store.create("用户喜欢美式咖啡")
        self.assertTrue(first["ok"])
        again = self.store.create("  用户喜欢美式咖啡  ")  # 空白差异也算重复
        self.assertFalse(again["ok"])
        self.assertEqual(again["existing_id"], first["id"])
        self.assertEqual(self.store.stats()["active"], 1)

    def test_edit_rejects_duplicate_of_other_item(self) -> None:
        first = self.store.create("用户喜欢美式")
        second = self.store.create("用户喜欢拿铁")
        clash = self.store.edit(second["id"], content="用户喜欢美式")
        self.assertFalse(clash["ok"])
        self.assertEqual(clash["existing_id"], first["id"])

    def test_edit_and_delete_unknown_id(self) -> None:
        self.assertFalse(self.store.edit(999, content="x")["ok"])
        self.assertFalse(self.store.delete(999)["ok"])
        self.assertFalse(self.store.edit(1)["ok"])  # 没给任何字段

    def test_enforces_max_items(self) -> None:
        for index in range(5):
            self.assertTrue(self.store.create(f"事实编号 {index}")["ok"])
        overflow = self.store.create("第六条")
        self.assertFalse(overflow["ok"])
        self.assertIn("上限", overflow["error"])

    def test_full_store_error_points_at_mem_edit(self) -> None:
        """满了要"满得响"：失败信息必须带可操作的下一步（ADR-057）。

        否则模型只会反复重试 mem_create —— 每次都被拒，白烧几步 token。
        """
        for index in range(5):
            self.store.create(f"事实编号 {index}")
        result = self.store.create("第六条")
        self.assertFalse(result["ok"])
        self.assertIn("记忆条数过多", result["error"])
        self.assertEqual(result["active_count"], 5)
        self.assertEqual(result["max_items"], 5)
        self.assertIn("mem_edit", result["hint"])

    # -- 已经删掉的机制：守卫 -----------------------------------------

    def test_alias_entry_points_are_gone_for_good(self) -> None:
        """别名机制随检索一起删除（ADR-058），字段本身也在 M7 迁移里删掉了（ADR-059）。

        这是一条**守卫**（同 `test_pricing.py` 的用意）：万一哪天有人把别名加回来，
        他必须先回答"谁负责读它" —— 现在没有任何读取方，写进去只会是死数据。
        """
        self.assertFalse(hasattr(self.store, "set_aliases"))
        self.assertFalse(hasattr(self.store, "format_dream_line"))
        with self.assertRaises(TypeError):
            self.store.create("小林会弹吉他", aliases=["乐器"])  # type: ignore[call-arg]

        memory_id = self.store.create("小林会弹吉他")["id"]
        item = self.store.get(memory_id)
        assert item is not None
        self.assertFalse(hasattr(item, "aliases"))
        columns = {
            row["name"] for row in self.store._conn.execute("PRAGMA table_info(memories)")
        }
        for dead in ("aliases", "sources", "dreamed_at", "dream_run"):
            self.assertNotIn(dead, columns, f"{dead} 又回来了？它没有任何读取方")

    # -- 注入渲染 ----------------------------------------------------

    def test_render_empty_and_format(self) -> None:
        self.assertIn("没有任何记忆", self.store.render_for_prompt())
        self.store.create("用户偏好中文", "偏好")
        self.store.create("用户在做一个 Harness 项目", "项目")
        rendered = self.store.render_for_prompt()
        lines = rendered.splitlines()
        # 第一行是"已用 N/上限 M"的表头（ADR-057 之后新增）：
        # 全量注入下，用户与模型都该看得见"离满还有多远"。
        self.assertEqual(lines[0], "（已用 2/5）")
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[1].startswith("[mem:"))
        self.assertIn("(偏好) 用户偏好中文", lines[1])
        # 默认按 id 升序，保证 prompt 前缀稳定
        self.assertLess(lines[1], lines[2])

    def test_render_stays_within_budget(self) -> None:
        for index in range(5):
            self.store.create(f"这是一条比较长的记忆内容编号为 {index} 用于撑爆预算")
        rendered = self.store.render_for_prompt(max_tokens=5)
        # 超预算时必须**如实说**少给了几条，不能悄悄少注入（ADR-058 保留了这条）
        self.assertIn("没有显示", rendered)
        self.assertLess(len(rendered), 200)

    def test_render_keeps_newest_when_overflowing(self) -> None:
        for index in range(5):
            self.store.create(f"内容 {index}")
        rendered = self.store.render_for_prompt(max_tokens=12)
        # 放不下时优先保留较新条目，但仍按 id 升序输出
        self.assertIn("[mem:5]", rendered)
        self.assertNotIn("[mem:1]", rendered)

    def test_context_is_recorded_in_ops(self) -> None:
        self.store.set_context("20261004-abc", 3)
        memory_id = self.store.create("带上下文的记忆")["id"]
        ops = self.store.ops_for(memory_id)
        self.assertEqual(ops[0]["session_id"], "20261004-abc")
        self.assertEqual(ops[0]["turn"], 3)


class MemoryMigrationTests(unittest.TestCase):
    """M7 的那次一次性迁移（ADR-059）。

    它做三件事：删 M4 留下的死列、删旧的 `dream_runs` 表、建 `dream_cursor`。
    这几条都值得钉住 —— 迁移写错的后果是**用户的记忆看起来被清空了**，
    那比报错更糟（他会以为数据没了，而不是以为程序坏了）。
    """

    # M4~M6 时期的 memories 表（多出四列），外加那时用来存快照的 dream_runs
    LEGACY = """
    CREATE TABLE memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'general',
        status TEXT NOT NULL DEFAULT 'active',
        revision INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        deleted_at TEXT,
        session_id TEXT,
        turn INTEGER,
        aliases TEXT,
        sources TEXT,
        dreamed_at TEXT,
        dream_run TEXT
    );
    CREATE INDEX idx_memories_status ON memories(status);
    CREATE TABLE memory_ops (
        id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id INTEGER NOT NULL,
        op TEXT NOT NULL, before TEXT, after TEXT, session_id TEXT, turn INTEGER,
        at TEXT NOT NULL
    );
    CREATE TABLE dream_runs (
        id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, mode TEXT,
        status TEXT, memories_seen INTEGER, snapshot TEXT, diff TEXT, summary TEXT,
        usage TEXT, stop_reason TEXT
    );
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _legacy(self, name: str = "memory.db") -> Path:
        path = self.tmp / name
        connection = sqlite3.connect(path)
        connection.executescript(self.LEGACY)
        connection.execute(
            "INSERT INTO memories (content, category, created_at, updated_at, aliases,"
            " sources, dreamed_at, dream_run) VALUES ('用户是中学生','身份','2026-10-01',"
            "'2026-10-01','[\"中学生\"]','[3]','2026-10-02','dream-1')"
        )
        connection.execute(
            "INSERT INTO memories (content, category, status, created_at, updated_at)"
            " VALUES ('用户会弹吉他','偏好','deleted','2026-10-01','2026-10-01')"
        )
        connection.execute(
            "INSERT INTO dream_runs (id, started_at, snapshot) VALUES ('dream-1','2026-10-02','[]')"
        )
        connection.commit()
        connection.close()
        return path

    @staticmethod
    def _columns(store: MemoryStore) -> set[str]:
        return {
            row["name"] for row in store._conn.execute("PRAGMA table_info(memories)")
        }

    @staticmethod
    def _tables(store: MemoryStore) -> set[str]:
        return {
            row["name"]
            for row in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    def test_old_database_is_migrated_in_place(self) -> None:
        path = self._legacy()
        store = MemoryStore(path, max_items=10, max_chars=100)
        try:
            # 数据一条不少，状态也照旧
            items = store.all_items()
            self.assertEqual([item.content for item in items], ["用户是中学生", "用户会弹吉他"])
            self.assertEqual([item.status for item in items], ["active", "deleted"])
            self.assertEqual(items[0].category, "身份")
            # 死列删掉、旧表删掉、游标建起来
            self.assertEqual(self._columns(store) & set(_DEAD_COLUMNS), set())
            self.assertNotIn("dream_runs", self._tables(store))
            self.assertIn("dream_cursor", self._tables(store))
            # 索引不能因为重建表而丢（丢了查询会随条目增多而变慢）
            indexes = {
                row["name"]
                for row in store._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
            self.assertIn("idx_memories_status", indexes)
            self.assertNotIn("memories_legacy", self._tables(store))
        finally:
            store.close()

    def test_migration_is_idempotent(self) -> None:
        path = self._legacy()
        for _ in range(3):
            store = MemoryStore(path, max_items=10, max_chars=100)
            self.assertEqual(len(store.all_items()), 2)
            self.assertEqual(self._columns(store) & set(_DEAD_COLUMNS), set())
            store.close()

    def test_interrupted_migration_is_repaired(self) -> None:
        """上一次迁移如果在"改名之后、复制之前"崩了，会留下 memories_legacy。

        这种半截状态会让用户看到一个**空**记忆库 —— 所以启动时必须自己收拾干净。
        """
        path = self._legacy("half.db")
        connection = sqlite3.connect(path)
        connection.execute("ALTER TABLE memories RENAME TO memories_legacy")
        connection.executescript(
            "CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " content TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'general',"
            " status TEXT NOT NULL DEFAULT 'active', revision INTEGER NOT NULL DEFAULT 1,"
            " created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT,"
            " session_id TEXT, turn INTEGER);"
        )
        connection.commit()
        connection.close()

        store = MemoryStore(path, max_items=10, max_chars=100)
        try:
            self.assertEqual(len(store.all_items()), 2, "半截迁移把用户的记忆弄丢了")
            self.assertNotIn("memories_legacy", self._tables(store))
        finally:
            store.close()

    def test_fresh_database_has_the_new_shape(self) -> None:
        """全新库走的是另一条代码路径（直接建表），必须单独测 —— 它曾经因为
        语句之间少写分号而在**全新安装**时直接抛语法错误。"""
        store = MemoryStore(self.tmp / "fresh.db", max_items=10, max_chars=100)
        try:
            self.assertEqual(self._tables(store) - {"sqlite_sequence"},
                             {"memories", "memory_ops", "dream_cursor"})
            self.assertEqual(store.stats(), {"active": 0, "deleted": 0, "ops": 0})
            self.assertEqual(store.cursor_map(), {})
        finally:
            store.close()

    def test_audit_trail_survives_the_migration(self) -> None:
        """`memory_ops` 是审计日志：迁移不能顺手把它清了。"""
        path = self._legacy("ops.db")
        connection = sqlite3.connect(path)
        connection.execute(
            "INSERT INTO memory_ops (memory_id, op, after, at)"
            " VALUES (1,'create','用户是中学生','2026-10-01')"
        )
        connection.commit()
        connection.close()
        store = MemoryStore(path, max_items=10, max_chars=100)
        try:
            self.assertEqual(store.stats()["ops"], 1)
        finally:
            store.close()


class DreamCursorTests(unittest.TestCase):
    """整理游标：每个会话"整理到第几条"。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = MemoryStore(
            Path(self._tmp.name) / "memory.db", max_items=10, max_chars=100
        )
        self.addCleanup(self.store.close)

    def test_cursor_is_per_session(self) -> None:
        self.store.advance_cursor("s-a", 12, "dream-1")
        self.store.advance_cursor("s-b", 3, "dream-1")
        self.assertEqual(self.store.cursor_map(), {"s-a": 12, "s-b": 3})

    def test_cursor_never_goes_backwards(self) -> None:
        """**只增不减**是硬的：调小只会让同样的内容被整理两遍（花两次钱），
        调大则会永久丢掉中间没读的记录。所以写入用 MAX()，任何调用方都改不回去。"""
        self.store.advance_cursor("s-a", 12, "dream-1")
        self.store.advance_cursor("s-a", 5, "dream-2")
        self.assertEqual(self.store.cursor_map()["s-a"], 12)
        self.store.advance_cursor("s-a", 20, "dream-3")
        self.assertEqual(self.store.cursor_map()["s-a"], 20)

    def test_blank_session_is_ignored(self) -> None:
        self.store.advance_cursor("", 5)
        self.store.advance_cursor("   ", 5)
        self.assertEqual(self.store.cursor_map(), {})

    def test_cursor_survives_reopen(self) -> None:
        self.store.advance_cursor("s-a", 7, "dream-1")
        self.store.close()
        again = MemoryStore(self.store.db_path, max_items=10, max_chars=100)
        try:
            self.assertEqual(again.cursor_map(), {"s-a": 7})
        finally:
            again.close()

    def test_cursor_does_not_touch_memories(self) -> None:
        """整理进度与记忆条目必须互不影响 —— 这条边界是 ADR-053 的核心。"""
        self.store.create("用户是中学生")
        before = self.store.stats()["active"]
        self.store.advance_cursor("s-a", 9, "dream-1")
        self.assertEqual(self.store.stats()["active"], before)
