"""工具配置文件（ADR-114）的存储语义测试。

覆盖四条铁律：
1. 只存启用的工具名（JSON 字符串数组）；
2. 默认配置坏/缺 → 按全部启用重建；会话配置坏/缺 → 复制默认配置；
3. 默认配置**永不直接**决定已出生会话（出生复制之后两者脱钩）；
4. 完整性在启动（check_all 自愈）与保存（validate 拒绝）两个场合分别检查。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.tool_config import (
    CHAT_TOOLS,
    ToolConfigError,
    ToolConfigStore,
    validate_config,
)

from .helpers import test_settings


class ValidateTests(unittest.TestCase):
    def test_all_known_names_pass(self) -> None:
        self.assertEqual(validate_config(list(CHAT_TOOLS)), sorted(CHAT_TOOLS))

    def test_empty_list_is_valid(self) -> None:
        """一个工具都不给是合法配置（用户有权让模型赤手空拳）。"""
        self.assertEqual(validate_config([]), [])

    def test_non_list_rejected(self) -> None:
        for bad in ({"a": 1}, "web_search", 42, None):
            with self.assertRaises(ToolConfigError):
                validate_config(bad)

    def test_unknown_name_rejected(self) -> None:
        with self.assertRaises(ToolConfigError) as ctx:
            validate_config(["web_search", "fs_rm_rf"])
        self.assertIn("fs_rm_rf", str(ctx.exception))

    def test_non_string_item_rejected(self) -> None:
        with self.assertRaises(ToolConfigError):
            validate_config(["web_search", 7])

    def test_duplicate_rejected(self) -> None:
        with self.assertRaises(ToolConfigError) as ctx:
            validate_config(["time", "time"])
        self.assertIn("time", str(ctx.exception))

    def test_output_is_sorted(self) -> None:
        self.assertEqual(validate_config(["web_search", "mem_create"]), ["mem_create", "web_search"])


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = test_settings(Path(self._tmp.name))
        self.store = ToolConfigStore(self.settings.data_dir)

    def test_default_config_is_born_all_enabled(self) -> None:
        enabled = self.store.ensure_default()
        self.assertEqual(enabled, sorted(CHAT_TOOLS))
        # 落盘且复读一致
        self.assertEqual(self.store.read_default(), sorted(CHAT_TOOLS))

    def test_corrupt_default_is_rebuilt_all_enabled(self) -> None:
        self.store.default_path.parent.mkdir(parents=True, exist_ok=True)
        self.store.default_path.write_text("not json{{", encoding="utf-8")
        self.assertEqual(self.store.ensure_default(), sorted(CHAT_TOOLS))

    def test_default_with_unknown_tool_is_rebuilt(self) -> None:
        """部分丢弃是静默降级：含未知名的默认配置整份作废重建。"""
        self.store.default_path.parent.mkdir(parents=True, exist_ok=True)
        self.store.default_path.write_text(json.dumps(["web_search", "nope"]), encoding="utf-8")
        self.assertEqual(self.store.ensure_default(), sorted(CHAT_TOOLS))

    def test_session_config_is_born_as_copy_of_default(self) -> None:
        self.store.save_default(["mem_create", "time"])
        born = self.store.ensure_session("s1")
        self.assertEqual(born, ["mem_create", "time"])
        # 文件真的在磁盘上，且与默认配置逐字同形
        on_disk = json.loads(self.store._session_path("s1").read_text(encoding="utf-8"))
        self.assertEqual(on_disk, ["mem_create", "time"])

    def test_default_changes_do_not_reach_born_sessions(self) -> None:
        """ADR-114 铁律：出生之后，默认配置怎么改都与这个会话无关。"""
        self.store.save_default(["mem_create"])
        self.store.ensure_session("s1")
        self.store.save_default(sorted(CHAT_TOOLS))  # 默认配置后来全开
        self.assertEqual(self.store.read_session("s1"), ["mem_create"])

    def test_corrupt_session_config_is_replaced_by_default_copy(self) -> None:
        self.store.save_default(["mem_create"])
        self.store.ensure_session("s1")
        self.store._session_path("s1").write_text("broken", encoding="utf-8")
        self.assertEqual(self.store.ensure_session("s1"), ["mem_create"])

    def test_missing_session_config_is_created_from_default(self) -> None:
        self.store.save_default(["time"])
        self.assertEqual(self.store.ensure_session("never-ran"), ["time"])

    def test_save_validates_and_normalizes(self) -> None:
        saved = self.store.save_session("s1", ["web_search", "mem_create", "time"])
        self.assertEqual(saved, ["mem_create", "time", "web_search"])
        self.assertEqual(self.store.read_session("s1"), saved)

    def test_save_rejects_invalid(self) -> None:
        with self.assertRaises(ToolConfigError):
            self.store.save_session("s1", ["web_search", "web_search"])
        # 拒绝 = 什么都没写
        self.assertIsNone(self.store.read_session("s1"))

    def test_bad_session_id_never_touches_disk(self) -> None:
        from harness.session_ids import validate_session_id

        with self.assertRaises(ValueError):
            validate_session_id("../escape")
        with self.assertRaises(ValueError):
            self.store.ensure_session("../escape")
        self.assertFalse((self.settings.data_dir / "tool_config").exists())

    def test_delete_session_removes_config(self) -> None:
        self.store.ensure_session("s1")
        self.assertTrue(self.store.delete_session("s1"))
        self.assertIsNone(self.store.read_session("s1"))
        self.assertFalse(self.store.delete_session("s1"))  # 已不在，返回 False

    def test_copy_session_forwards_config_to_fork(self) -> None:
        self.store.save_session("src", ["mem_create"])
        self.assertEqual(self.store.copy_session("src", "dst"), ["mem_create"])
        self.assertEqual(self.store.read_session("dst"), ["mem_create"])

    def test_copy_session_falls_back_to_default_when_source_has_none(self) -> None:
        """分叉一个出生前的老会话：新会话按出生规则复制默认配置。"""
        self.store.save_default(["time"])
        self.assertEqual(self.store.copy_session("old", "dst"), ["time"])

    def test_read_only_methods_never_create_files(self) -> None:
        """"看一眼不落盘"对配置同样成立。"""
        self.assertIsNone(self.store.read_default())
        self.assertIsNone(self.store.read_session("ghost"))
        self.assertFalse(self.settings.data_dir.joinpath("tool_config.json").exists())
        self.assertFalse((self.settings.data_dir / "tool_config").exists())


class CheckAllTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = test_settings(Path(self._tmp.name))
        self.store = ToolConfigStore(self.settings.data_dir)
        self.settings.sessions_dir.mkdir(parents=True, exist_ok=True)

    def _mk_session(self, name: str) -> None:
        (self.settings.sessions_dir / f"{name}.jsonl").write_text(
            '{"seq": 1, "kind": "user_message", "text": "hi"}\n', encoding="utf-8"
        )

    def test_check_all_repairs_everything(self) -> None:
        self._mk_session("s1")
        self._mk_session("s2")
        self.store.save_session("s2", ["mem_create"])
        self.store._session_path("s2").write_text("broken", encoding="utf-8")
        self.store.default_path.write_text("[] extra", encoding="utf-8")

        repairs = self.store.check_all(self.settings.sessions_dir)
        self.assertTrue(repairs)  # 有修复就要有说明（不许静默自愈）
        self.assertEqual(self.store.read_default(), sorted(CHAT_TOOLS))
        self.assertEqual(self.store.read_session("s1"), sorted(CHAT_TOOLS))
        self.assertEqual(self.store.read_session("s2"), sorted(CHAT_TOOLS))

    def test_check_all_is_idempotent(self) -> None:
        self._mk_session("s1")
        self.store.check_all(self.settings.sessions_dir)
        self.assertEqual(self.store.check_all(self.settings.sessions_dir), [])

    def test_check_all_ignores_non_session_files(self) -> None:
        """目录里的杂文件（非会话命名）不许触发任何修复动作。"""
        self.store.ensure_default()  # 先把默认配置摆好，隔离"默认重建"这条修复
        (self.settings.sessions_dir / "junk.txt").write_text("x", encoding="utf-8")
        (self.settings.sessions_dir / "bad..name.jsonl").write_text("x", encoding="utf-8")
        self.assertEqual(self.store.check_all(self.settings.sessions_dir), [])


if __name__ == "__main__":
    unittest.main()
