"""原生 memory_instruction.md 与 Dream 游标测试。"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from harness.memory import (
    MEMORY_HEADER,
    MemoryFormatError,
    MemoryItem,
    MemoryStore,
    parse_memory_instructions,
    read_memory_instructions,
    render_memory_instructions,
)


class NativeMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.instruction = self.tmp / "vault" / "memory_instruction.md"
        self.cursor_db = self.tmp / "memory.db"
        self.store = self.make_store()
        self.addCleanup(self.store.close)

    def make_store(self, *, max_items: int = 5, max_chars: int = 80) -> MemoryStore:
        return MemoryStore(
            instruction_path=self.instruction,
            cursor_db_path=self.cursor_db,
            max_items=max_items,
            max_chars=max_chars,
        )

    def test_open_does_not_create_markdown(self) -> None:
        self.assertFalse(self.instruction.exists(), "看一眼不能创建原生记忆文件")
        self.assertEqual(self.store.active_items(), [])
        self.assertEqual(self.store.stats(), {"active": 0})

    def test_create_edit_delete_round_trip(self) -> None:
        created = self.store.create("用户喜欢美式咖啡")
        self.assertEqual(
            set(created), {"ok", "id", "content", "active_count", "max_items", "note"}
        )
        self.assertTrue(created["ok"])
        memory_id = created["id"]
        self.assertGreater(memory_id, 0)
        self.assertLess(memory_id, 2**53, "Web 端数字必须保持为 JS 安全整数")

        edited = self.store.edit(memory_id, content="用户更喜欢拿铁")
        self.assertEqual(set(edited), {"ok", "id", "content", "note"})
        self.assertEqual(self.store.get(memory_id).content, "用户更喜欢拿铁")

        deleted = self.store.delete(memory_id, reason="用户要求忘记")
        self.assertTrue(deleted["ok"])
        self.assertEqual(deleted["forgot"], "用户更喜欢拿铁")
        self.assertEqual(deleted["reason"], "用户要求忘记")
        self.assertIsNone(self.store.get(memory_id))
        self.assertEqual(self.store.stats(), {"active": 0})
        # 删除是硬删除：文件里没有另一份 deleted/status 副本。
        text = self.instruction.read_text(encoding="utf-8")
        self.assertNotIn("拿铁", text)
        self.assertNotIn("deleted", text)
        self.assertEqual(read_memory_instructions(self.instruction), [])

    def test_file_has_native_shape_and_persists_across_reopen(self) -> None:
        first = self.store.create("第一条记忆")
        second = self.store.create("第二条记忆")
        self.store.close()
        text = self.instruction.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(MEMORY_HEADER + "\n"))
        self.assertIn(f"## {first['id']}\n\n第一条记忆", text)
        self.assertIn(f"## {second['id']}\n\n第二条记忆", text)

        reopened = self.make_store()
        try:
            self.assertEqual(
                [(item.id, item.content) for item in reopened.active_items()],
                [(first["id"], "第一条记忆"), (second["id"], "第二条记忆")],
            )
        finally:
            reopened.close()

    def test_parse_render_preserves_sparse_ids_file_order_and_multiline_body(self) -> None:
        items = [
            MemoryItem(id=2, content="第二\n\n段"),
            MemoryItem(id=1, content="第一"),
            MemoryItem(id=17, content="第十七"),
        ]
        rendered = render_memory_instructions(items)
        self.assertEqual(parse_memory_instructions(rendered), items)
        self.assertLess(rendered.index("## 2\n"), rendered.index("## 1\n"))
        self.assertLess(rendered.index("## 1\n"), rendered.index("## 17\n"))

    def test_create_appends_and_edit_keeps_native_file_order(self) -> None:
        self.instruction.parent.mkdir(parents=True, exist_ok=True)
        self.instruction.write_text(
            "# Memory instructions\n\n## 2\n\n旧二\n\n## 1\n\n旧一\n",
            encoding="utf-8",
        )
        self.store.edit(2, "改二")
        created = self.store.create("新三")
        items = read_memory_instructions(self.instruction)
        self.assertEqual([item.id for item in items], [2, 1, created["id"]])
        self.assertEqual(items[0].content, "改二")

    def test_deleted_highest_id_is_not_immediately_reused(self) -> None:
        first = self.store.create("一")
        second = self.store.create("二")
        self.store.delete(second["id"])
        third = self.store.create("三")
        self.assertGreater(third["id"], second["id"])
        self.assertNotEqual(third["id"], first["id"])

    def test_rejects_empty_too_long_and_delimiter_collision(self) -> None:
        self.assertFalse(self.store.create("")["ok"])
        self.assertFalse(self.store.create("   ")["ok"])
        self.assertFalse(self.store.create("很" * 81)["ok"])
        for content in ("## 2", "第一行\n## 23\n第二行"):
            with self.subTest(content=content):
                result = self.store.create(content)
                self.assertFalse(result["ok"])
                self.assertIn("独立行", result["error"])
        self.assertEqual(self.store.stats(), {"active": 0})

    def test_rejects_duplicate_create_and_edit(self) -> None:
        first = self.store.create("用户喜欢美式咖啡")
        second = self.store.create("用户喜欢拿铁")
        again = self.store.create("  用户喜欢美式咖啡  ")
        self.assertFalse(again["ok"])
        self.assertEqual(again["existing_id"], first["id"])
        clash = self.store.edit(second["id"], "用户喜欢美式咖啡")
        self.assertFalse(clash["ok"])
        self.assertEqual(clash["existing_id"], first["id"])
        self.assertEqual(self.store.stats(), {"active": 2})

    def test_edit_requires_content_and_unknown_ids_fail(self) -> None:
        memory_id = self.store.create("一条")["id"]
        self.assertFalse(self.store.edit(memory_id)["ok"])
        self.assertFalse(self.store.edit(999, "x")["ok"])
        self.assertFalse(self.store.delete(999)["ok"])

    def test_enforces_max_items_and_unchanged_edit(self) -> None:
        for index in range(5):
            self.assertTrue(self.store.create(f"事实 {index}")["ok"])
        overflow = self.store.create("第六条")
        self.assertFalse(overflow["ok"])
        self.assertEqual(overflow["active_count"], 5)
        self.assertIn("mem_edit", overflow["hint"])
        item = self.store.active_items()[0]
        unchanged = self.store.edit(item.id, item.content)
        self.assertEqual(unchanged["note"], "内容没有变化，未做修改")

    def test_strict_malformed_files_fail_closed(self) -> None:
        cases = {
            "missing_header": "## 1\n\n内容\n",
            "junk": "# Memory instructions\n\n杂项\n## 1\n\n内容\n",
            "duplicate_id": "# Memory instructions\n\n## 1\n\n一\n\n## 1\n\n二\n",
            "empty_item": "# Memory instructions\n\n## 1\n\n\n## 2\n\n二\n",
            "bad_heading": "# Memory instructions\n\n## zero\n\n一\n",
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(MemoryFormatError):
                    parse_memory_instructions(text)

    def test_old_sqlite_is_neither_read_nor_migrated(self) -> None:
        with closing(sqlite3.connect(self.cursor_db)) as connection:
            connection.execute("CREATE TABLE memories (id INTEGER, content TEXT)")
            connection.execute("INSERT INTO memories VALUES (1, '旧 SQLite 记忆')")
            connection.commit()
        self.instruction.parent.mkdir(parents=True, exist_ok=True)
        self.instruction.write_text("# Memory instructions\n\n## 7\n\n原生记忆\n", encoding="utf-8")

        items = self.store.active_items()
        self.assertEqual([(item.id, item.content) for item in items], [(7, "原生记忆")])
        with closing(sqlite3.connect(self.cursor_db)) as connection:
            self.assertEqual(
                connection.execute("SELECT content FROM memories").fetchone()[0],
                "旧 SQLite 记忆",
                "不迁移也不借清理之名改写旧数据",
            )
        self.assertEqual(
            self.store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_ops'"
            ).fetchall(),
            [],
        )

    def test_only_cursor_table_is_created_for_a_fresh_db(self) -> None:
        names = {
            row["name"]
            for row in self.store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertEqual(names - {"sqlite_sequence"}, {"dream_cursor"})

    def test_render_for_prompt_uses_native_id_content_shape(self) -> None:
        self.store.create("用户偏好中文")
        self.store.create("用户在做一个 Harness 项目")
        rendered = self.store.render_for_prompt()
        lines = rendered.splitlines()
        self.assertEqual(lines[0], "（已用 2/5）")
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[1].startswith("[mem:"))
        self.assertIn("用户偏好中文", lines[1])
        self.assertNotIn("category", lines[1])
        self.assertLess(lines[1], lines[2])

    def test_injection_budget_and_snapshot_are_consistent(self) -> None:
        for index in range(5):
            self.store.create(f"较长的记忆内容 {index} 用来撑预算")
        text, omitted, total = self.store.injection_view(max_tokens=12)
        self.assertEqual(total, 5)
        self.assertGreater(omitted, 0)
        self.assertIn("没有显示", text)
        self.assertLess(len(text), 240)

    def test_concurrent_stores_cannot_create_duplicates_or_exceed_cap(self) -> None:
        def race(contents: tuple[str, str], cap: int, name: str) -> list[dict]:
            instruction = self.tmp / f"{name}.md"
            cursor_db = self.tmp / f"{name}.db"
            barrier = threading.Barrier(2)

            class RacingStore(MemoryStore):
                def _read_items(self):
                    if not self._conn.in_transaction:
                        barrier.wait(timeout=5)
                    return super()._read_items()

            def create_one(content: str) -> dict:
                store = RacingStore(
                    instruction_path=instruction,
                    cursor_db_path=cursor_db,
                    max_items=cap,
                    max_chars=80,
                )
                try:
                    return store.create(content)
                finally:
                    store.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                return list(pool.map(create_one, contents))

        duplicates = race(("同一条记忆", "  同一条记忆  "), 5, "duplicates")
        self.assertEqual(sum(bool(item["ok"]) for item in duplicates), 1)
        capped = race(("第一条", "第二条"), 1, "capped")
        self.assertEqual(sum(bool(item["ok"]) for item in capped), 1)
        self.assertEqual(self.store.stats(), {"active": 0})
        self.assertEqual(
            parse_memory_instructions((self.tmp / "capped.md").read_text(encoding="utf-8")),
            read_memory_instructions(self.tmp / "capped.md"),
        )


class DreamCursorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = MemoryStore(
            instruction_path=Path(self._tmp.name) / "vault" / "memory_instruction.md",
            cursor_db_path=Path(self._tmp.name) / "memory.db",
            max_items=10,
            max_chars=100,
        )
        self.addCleanup(self.store.close)

    def test_cursor_is_per_session_and_persistent(self) -> None:
        self.store.advance_cursor("s-a", 12, "dream-1")
        self.store.advance_cursor("s-b", 3, "dream-1")
        self.assertEqual(self.store.cursor_map(), {"s-a": 12, "s-b": 3})
        self.store.advance_cursor("s-a", 5, "dream-2")
        self.assertEqual(self.store.cursor_map()["s-a"], 12)
        self.store.advance_cursor("s-a", 20, "dream-3")
        self.assertEqual(self.store.cursor_map()["s-a"], 20)
        self.store.close()
        again = MemoryStore(
            instruction_path=self.store.instruction_path,
            cursor_db_path=self.store.db_path,
        )
        try:
            self.assertEqual(again.cursor_map()["s-a"], 20)
        finally:
            again.close()

    def test_blank_session_is_ignored(self) -> None:
        self.store.advance_cursor("", 5)
        self.store.advance_cursor("   ", 5)
        self.assertEqual(self.store.cursor_map(), {})

    def test_cursor_does_not_touch_native_memories(self) -> None:
        created = self.store.create("独立记忆")
        self.store.advance_cursor("s-a", 7, "dream-1")
        self.assertEqual(self.store.active_items()[0].content, "独立记忆")
        self.store.clear_cursors()
        self.assertEqual(self.store.cursor_map(), {})
        self.assertEqual(self.store.active_items()[0].id, created["id"])


if __name__ == "__main__":
    unittest.main()
