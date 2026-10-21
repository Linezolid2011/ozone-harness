"""发布相关的守卫：版本号只有一个来源，changelog 必须跟着它。

为什么值得单独一组测试：版本号写死的后果不是"不好看"，是**自相矛盾** ——
界面说 `1.0.0-rc1`、命令行说 `0.1.0`，而用户问"我跑的到底是哪个"时两个答案都不算数。
所以这里钉三件事：

1. `docs/version.txt` 是**唯一来源**（`harness.version()` 读它，界面与 CLI 都从那里取）；
2. `docs/changelog.md` 里必须出现当前版本号（升号忘了写 changelog，测试就红）；
3. 发布要带的几个文件都在（`version.txt` / `changelog.md` / `AGENTS.md`）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import harness


class VersionTests(unittest.TestCase):
    def test_reads_the_shipped_file(self) -> None:
        shipped = harness.VERSION_FILE.read_text(encoding="utf-8").strip()
        self.assertEqual(harness.version(), shipped)
        self.assertTrue(harness.version(), "版本号不能是空串")
        self.assertNotEqual(
            harness.version(), harness.FALLBACK_VERSION, "发出去的包里不该还是兜底版本"
        )

    def test_only_the_first_line_counts(self) -> None:
        """手写这个文件时后面很容易多打一行备注 —— 那一行不该变成版本号。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v.txt"
            path.write_text("\ufeff2.3.4\n# 备注\n", encoding="utf-8")
            self.assertEqual(harness.version(path), "2.3.4")

    def test_missing_or_empty_falls_back_instead_of_crashing(self) -> None:
        """版本号读不到不该让程序起不来：退回兜底值，界面照常打开。"""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                harness.version(Path(tmp) / "nope.txt"), harness.FALLBACK_VERSION
            )
            empty = Path(tmp) / "empty.txt"
            empty.write_text("   \n", encoding="utf-8")
            self.assertEqual(harness.version(empty), harness.FALLBACK_VERSION)

    def test_import_time_alias_matches(self) -> None:
        self.assertEqual(harness.__version__, harness.version())


class ReleaseFilesTests(unittest.TestCase):
    def test_changelog_mentions_the_current_version(self) -> None:
        """升了版本号却没写 changelog —— 这是发布前最容易漏的一步。"""
        changelog = (harness.ROOT / "docs" / "changelog.md").read_text(encoding="utf-8")
        self.assertIn(harness.version(), changelog)

    def test_release_files_are_present(self) -> None:
        for name in ("version.txt", "changelog.md", "AGENTS.md"):
            with self.subTest(name=name):
                path = harness.ROOT / "docs" / name
                self.assertTrue(path.is_file(), f"缺 docs/{name}")
                self.assertTrue(path.read_text(encoding="utf-8").strip())

    def test_agents_md_states_the_essentials(self) -> None:
        """AGENTS.md 是给"下一个 agent"的说明书：这些规矩必须在里面写清楚。"""
        text = (harness.ROOT / "docs" / "AGENTS.md").read_text(encoding="utf-8")
        for needle in ("ADR", "unittest", "离线", "绝对路径", "version.txt"):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)
