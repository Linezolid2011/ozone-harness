"""工具分组树（ADR-115）测试：随 harness 发布的 tool_groups.json。

钉住三件事：

1. shipped 文件合法且恰好划分 CHAT_TOOLS（一个工具一组、无未知、无遗漏）；
2. shipped 文件与内置默认一致（运行时的 fail-soft 回退是 resilience，
   shipped 文件漂移是开发期错误 —— 两者靠这条测试分开）；
3. 加载器对坏输入 fail-closed 回退，不抛异常（分组是布局，不值得弄死服务）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.tool_config import CHAT_TOOLS
from harness.tool_groups import (
    DEFAULT_GROUPS,
    GROUPS_FILE,
    ToolGroup,
    _default_groups,
    _parse,
    load_groups,
)


class ShippedFileTests(unittest.TestCase):
    def test_shipped_file_parses_and_partitions_chat_tools(self) -> None:
        groups = load_groups(quiet=True)
        self.assertTrue(groups, "shipped 分组文件至少要有一组")
        grouped = [name for group in groups for name in group.tools]
        # 恰好划分：不多（重复/未知在校验里就炸了）、不少
        self.assertEqual(sorted(grouped), sorted(CHAT_TOOLS))
        self.assertEqual(len(grouped), len(set(grouped)), "一个工具只能属一组")

    def test_shipped_file_matches_builtin_default(self) -> None:
        """漂移即红：回退默认是运行时的安全网，不是"改代码不用改文件"的许可证。"""
        shipped = [(group.id, tuple(group.tools)) for group in load_groups(quiet=True)]
        self.assertEqual(shipped, list(DEFAULT_GROUPS))

    def test_group_ids_are_stable_contracts(self) -> None:
        """组 id 是 i18n 键（ui.toolconfig.groups.<id>）与前端状态（openGroups）
        的契约：改名 = 改三处，所以这里钉住当前名单。"""
        ids = [group.id for group in load_groups(quiet=True)]
        self.assertEqual(ids, ["memory", "dream", "web", "general"])

    def test_group_display_names_exist_in_both_languages(self) -> None:
        from harness import i18n
        from harness.i18n import t

        for group in load_groups(quiet=True):
            for language in ("zh", "en"):
                with i18n.use(language):
                    label = t(f"ui.toolconfig.groups.{group.id}")
                self.assertTrue(label and label != f"ui.toolconfig.groups.{group.id}",
                                f"{language} 缺组显示名 {group.id}")


class LoaderFallbackTests(unittest.TestCase):
    def _write(self, content: str) -> Path:
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        path = Path(tmp) / "tool_groups.json"
        path.write_text(content, encoding="utf-8")
        return path

    def test_missing_file_falls_back_to_builtin(self) -> None:
        groups = load_groups(Path("/nonexistent/tool_groups.json"), quiet=True)
        self.assertEqual(groups, _default_groups())

    def test_corrupt_json_falls_back(self) -> None:
        path = self._write("not json{{")
        self.assertEqual(load_groups(path, quiet=True), _default_groups())

    def test_unknown_tool_falls_back(self) -> None:
        data = {"version": 1, "groups": [{"id": "x", "tools": ["mem_create", "fs_rm_rf"]}]}
        path = self._write(json.dumps(data, ensure_ascii=False))
        self.assertEqual(load_groups(path, quiet=True), _default_groups())

    def test_duplicate_grouping_falls_back(self) -> None:
        data = {
            "version": 1,
            "groups": [
                {"id": "a", "tools": ["mem_create", "mem_edit"]},
                {"id": "b", "tools": ["mem_del"]},  # mem_del 已经…不，这里是漏其余工具
            ],
        }
        path = self._write(json.dumps(data, ensure_ascii=False))
        self.assertEqual(load_groups(path, quiet=True), _default_groups())

    def test_tool_in_two_groups_falls_back(self) -> None:
        data = {
            "version": 1,
            "groups": [
                {"id": "a", "tools": ["mem_create", "time"]},
                {"id": "b", "tools": ["time"]},
            ],
        }
        path = self._write(json.dumps(data, ensure_ascii=False))
        self.assertEqual(load_groups(path, quiet=True), _default_groups())

    def test_parse_error_messages_name_the_problem(self) -> None:
        with self.assertRaisesRegex(ValueError, "未知工具"):
            _parse({"groups": [{"id": "a", "tools": ["nope"]}]})
        with self.assertRaisesRegex(ValueError, "多个分组"):
            _parse({"groups": [
                {"id": "a", "tools": ["time"]},
                {"id": "b", "tools": ["time"]},
            ]})
        with self.assertRaisesRegex(ValueError, "没有分组"):
            _parse({"groups": [{"id": "a", "tools": ["time"]}]})

    def test_shipped_file_has_bom_tolerance(self) -> None:
        """用户手改 shipped 文件后记事本可能加 BOM —— 读端必须认（ADR-085 口径）。"""
        raw = GROUPS_FILE.read_bytes()
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        path = Path(tmp) / "tool_groups.json"
        path.write_bytes(b"\xef\xbb\xbf" + raw)
        self.assertEqual(load_groups(path, quiet=True), _default_groups())


if __name__ == "__main__":
    unittest.main()
