"""记忆条数上限（ADR-057）：纯函数校验、运行期配置的读写、与 `open_memory` 的接线。

上限是**用户会经常改**的运行期配置，所以它有三条各自独立的规则：
整数、0–100、**不能低于当前条数**。三条规则都落在 `validate_limit` 这个纯函数里，
于是可以在这里逐条离线钉死，不需要数据库、不需要模型。

最后一段测的是"接线"：`open_memory()` 是**唯一**的构造入口，
它必须真的去读 `data/memory.json` —— 否则就会出现"设置里改了、界面也显示改了、
行为却没变"这种最难查的 bug。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.memory import open_memory
from harness.memory_limit import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MEMORY_FILE,
    MIN_LIMIT,
    MemoryLimitStore,
    limit_for,
    validate_limit,
)

from .helpers import test_settings


class ValidateLimitTests(unittest.TestCase):
    """纯函数：不碰磁盘、不碰数据库，所以坏输入可以穷举。"""

    def test_boundaries_are_allowed(self) -> None:
        for value in (MIN_LIMIT, 1, 50, MAX_LIMIT):
            with self.subTest(value=value):
                change = validate_limit(value, active_count=0)
                self.assertTrue(change.ok, change.error)
                self.assertEqual(change.limit, value)

    def test_zero_is_a_legal_setting(self) -> None:
        """0 是"什么都别记"的合法设置，不是"没填"。"""
        change = validate_limit(0, active_count=0)
        self.assertTrue(change.ok)
        self.assertEqual(change.limit, 0)

    def test_out_of_range_is_rejected(self) -> None:
        for value in (-1, MAX_LIMIT + 1, 1000):
            with self.subTest(value=value):
                change = validate_limit(value, active_count=0)
                self.assertFalse(change.ok)
                self.assertIn("0 到 100", change.error)

    def test_below_active_count_is_rejected_with_the_count(self) -> None:
        """低于当前条数 = "瞬间让若干条记忆消失"，必须拒绝并说明原因。"""
        change = validate_limit(3, active_count=5)
        self.assertFalse(change.ok)
        self.assertIn("上限太低", change.error)
        self.assertIn("5 条", change.error)
        self.assertEqual(change.limit, 0)

    def test_equal_to_active_count_is_allowed(self) -> None:
        """边界：等于当前条数是允许的（这是最自然的"我不想再加了"）。"""
        change = validate_limit(5, active_count=5)
        self.assertTrue(change.ok)
        self.assertEqual(change.limit, 5)

    def test_illegal_inputs_are_rejected(self) -> None:
        for value in ("abc", "", None, [], {}, object()):
            with self.subTest(value=value):
                change = validate_limit(value, active_count=0)
                self.assertFalse(change.ok)
                self.assertIn("整数", change.error)

    def test_booleans_are_not_integers(self) -> None:
        """`True` 能被 `int()` 变成 1 —— 那是 PHP 式的坑，必须显式拒绝。"""
        for value in (True, False):
            with self.subTest(value=value):
                self.assertFalse(validate_limit(value, active_count=0).ok)

    def test_numeric_strings_are_accepted(self) -> None:
        """表单/JSON 里来的数字常常是字符串，这一层要能收。"""
        change = validate_limit("20", active_count=1)
        self.assertTrue(change.ok)
        self.assertEqual(change.limit, 20)

    def test_fractional_values_are_rejected_not_truncated(self) -> None:
        """`int(3.7)` 会静默变成 3 —— 用户设了 3.7 却得到 3 还不报错。

        这是这个项目一直在清的那类"看起来成功了"的失败（对比 ADR-057 的
        "满了要满得响"）。整数值的浮点（JSON 里的 `50.0`）仍然接受。
        """
        for value in (3.7, "3.7", 0.5):
            with self.subTest(value=value):
                change = validate_limit(value, active_count=0)
                self.assertFalse(change.ok)
                self.assertIn("整数", change.error)
        integral = validate_limit(50.0, active_count=0)
        self.assertTrue(integral.ok)
        self.assertEqual(integral.limit, 50)

    def test_to_dict_carries_the_error_only_when_there_is_one(self) -> None:
        self.assertEqual(validate_limit(10, active_count=0).to_dict(), {"ok": True, "limit": 10})
        failed = validate_limit(200, active_count=0).to_dict()
        self.assertFalse(failed["ok"])
        self.assertIn("error", failed)


class MemoryLimitStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        self.path = self.data_dir / MEMORY_FILE

    def store(self, **kwargs) -> MemoryLimitStore:
        return MemoryLimitStore(self.path, **kwargs)

    def test_reading_never_creates_the_file(self) -> None:
        """界面上"看一眼"不能改磁盘（同 `preview_reset` / `VaultStore` 的原则）。"""
        store = self.store()
        self.assertEqual(store.limit(), DEFAULT_LIMIT)
        self.assertFalse(self.path.exists(), "只读居然把配置文件种出来了")
        store.public_state(active_count=3)
        self.assertFalse(self.path.exists())

    def test_saved_value_takes_effect(self) -> None:
        store = self.store()
        change = store.set(30, active_count=0)
        self.assertTrue(change.ok)
        self.assertEqual(store.limit(), 30)
        self.assertTrue(self.path.is_file())

        # 重新打开（模拟"下一次构造 MemoryStore"）也要读到同一个值
        self.assertEqual(self.store().limit(), 30)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data, {"max_items": 30})

    def test_rejected_value_is_not_written(self) -> None:
        store = self.store()
        self.assertFalse(store.set(2, active_count=5).ok)
        self.assertEqual(store.limit(), DEFAULT_LIMIT)
        self.assertFalse(self.path.exists(), "被拒绝的值不该落盘")

    def test_broken_json_falls_back_to_the_default(self) -> None:
        """坏 JSON 只可能是被手工改坏的：退回默认值比"整个程序起不来"好。"""
        for raw in ("{ 不是 json", "", "[1, 2, 3]", "null"):
            with self.subTest(raw=raw):
                self.path.write_text(raw, encoding="utf-8")
                self.assertEqual(self.store().limit(), DEFAULT_LIMIT)

    def test_out_of_range_value_on_disk_is_clamped(self) -> None:
        for raw, expected in ((999, MAX_LIMIT), (-5, MIN_LIMIT), ("x", DEFAULT_LIMIT)):
            with self.subTest(raw=raw):
                self.path.write_text(json.dumps({"max_items": raw}), encoding="utf-8")
                self.assertEqual(self.store().limit(), expected)

    def test_default_is_clamped_to_the_legal_range(self) -> None:
        self.assertEqual(self.store(default=999).limit(), MAX_LIMIT)
        self.assertEqual(self.store(default=-1).limit(), MIN_LIMIT)

    def test_public_state_reports_the_lowest_settable_value(self) -> None:
        """界面要知道"最低能设到几"，否则用户只会觉得这个框不让他改。"""
        self.assertEqual(
            self.store().public_state(active_count=7),
            {
                "max_items": DEFAULT_LIMIT,
                "active_count": 7,
                "min_allowed": MIN_LIMIT,
                "max_allowed": MAX_LIMIT,
                "lowest_now": 7,
            },
        )

    def test_limit_for_reads_the_same_file(self) -> None:
        self.assertEqual(limit_for(self.data_dir), DEFAULT_LIMIT)
        self.store().set(12, active_count=0)
        self.assertEqual(limit_for(self.data_dir), 12)
        self.assertEqual(limit_for(self.data_dir, 3), 12, "文件在就听文件的")


class OpenMemoryTests(unittest.TestCase):
    """**接线**：`open_memory()` 必须真的读运行期上限（ADR-057 的唯一构造入口）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = test_settings(Path(self._tmp.name))

    def test_open_memory_uses_the_seed_limit_when_nothing_is_saved(self) -> None:
        memory = open_memory(self.settings)
        try:
            self.assertEqual(memory.max_items, self.settings.memory_limit)
        finally:
            memory.close()

    def test_open_memory_follows_a_saved_limit(self) -> None:
        limit_path = self.settings.data_dir / MEMORY_FILE
        MemoryLimitStore(limit_path, default=self.settings.memory_limit).set(
            7, active_count=0
        )

        memory = open_memory(self.settings)
        try:
            self.assertEqual(memory.max_items, 7, "改了上限却没生效？")
            for index in range(7):
                self.assertTrue(memory.create(f"事实编号 {index}")["ok"])
            overflow = memory.create("第八条")
            self.assertFalse(overflow["ok"])
            self.assertIn("记忆条数过多", overflow["error"])
        finally:
            memory.close()

    def test_a_hand_edited_low_limit_never_hides_existing_memories(self) -> None:
        """有人把 `memory.json` 手改成比现有条数还小：上限**只管新建**。

        已有记忆仍然全部注入（`injection_plan` 只看 token 预算）。
        否则就会出现"记忆没被删、却再也不进上下文"那种最糟的中间状态 ——
        严格拒绝"低于现有条数"是**设置界面那条路**的职责（`validate_limit`）。
        """
        memory = open_memory(self.settings)
        try:
            for index in range(5):
                memory.create(f"事实编号 {index}")
        finally:
            memory.close()

        (self.settings.data_dir / MEMORY_FILE).write_text(
            json.dumps({"max_items": 2}), encoding="utf-8"
        )

        memory = open_memory(self.settings)
        try:
            self.assertEqual(memory.max_items, 2)
            self.assertEqual(len(memory.active_items()), 5, "已有记忆一条都不能少")
            items, omitted = memory.injection_plan(max_tokens=6000)
            self.assertEqual(len(items), 5, "上限不该用于隐藏已有记忆")
            self.assertEqual(omitted, 0)
            self.assertFalse(memory.create("第六条")["ok"], "新建才受上限管")
        finally:
            memory.close()


if __name__ == "__main__":
    unittest.main()
