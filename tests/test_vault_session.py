"""会话 agent 编辑 dream vault（ADR-111）的离线回归。

重点不是"能写文件"本身，而是**只能**写允许的范围：保护清单、生成物、
独立记忆与 raw 素材不能通过直接路径或父目录递归删除被绕过。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness import i18n
from harness.runtime import Runtime
from harness.tools.registry import ToolRegistry
from harness.tools.vault_edit_tools import build_session_vault_edit_tools
from harness.tools.vault_tools import VaultListTool, VaultReadTool, VaultSearchTool
from harness.vault import (
    INDEX_NAME,
    LOG_NAME,
    MEMORY_INSTRUCTION_NAME,
    OBSERVATIONS_NAME,
    README_NAME,
    SECTIONS_CONFIG,
    VaultStore,
)

from .helpers import FakeProvider, test_settings, text_reply

CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")


def _make_dir_link(link: Path, target: Path) -> bool:
    """优先 symlink；Windows 无权限时退回 junction（覆盖 os.path.islink 的盲区）。"""
    try:
        link.symlink_to(target, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name != "nt":
        return False
    try:
        completed = subprocess.run(  # noqa: S603 - 固定命令，路径来自一次性目录
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and link.exists()


class SessionVaultCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = VaultStore(self.root / "vault")
        self.registry = ToolRegistry(max_result_chars=4000)
        for tool in (
            VaultListTool(self.store),
            VaultReadTool(self.store),
            VaultSearchTool(self.store),
            *build_session_vault_edit_tools(self.store),
        ):
            self.registry.register(tool)

    def call(self, name: str, **args):
        return self.registry.call(name, json.dumps(args, ensure_ascii=False))


class SessionVaultWriteTests(SessionVaultCase):
    def test_create_edit_read_search_and_index_nested_archive(self) -> None:
        created = self.call(
            "dream_create",
            path="entities/research/小林.md",
            body="喜欢手冲咖啡。",
            title="小林",
            tags=["person"],
        )
        self.assertTrue(created.ok, created.payload)
        self.assertEqual(created.payload["path"], "entities/research/小林.md")
        self.assertTrue(created.payload["regenerated"])
        self.assertTrue((self.store.root / INDEX_NAME).is_file())
        self.assertFalse(
            (self.store.root / SECTIONS_CONFIG).exists(),
            "会话写旧 vault 不能顺手把它迁移成原生模式",
        )

        read = self.call("dream_read", path="entities/research/小林.md")
        self.assertTrue(read.ok, read.payload)
        self.assertEqual(read.payload["body"], "喜欢手冲咖啡。")
        self.assertEqual(read.payload["frontmatter"]["title"], "小林")
        self.assertEqual(self.call("dream_list").payload["count"], 1)
        self.assertEqual(self.call("dream_search", query="手冲").payload["count"], 1)
        self.assertIn("entities/research/小林", self.store.read(INDEX_NAME).body)

        edited = self.call(
            "dream_edit", path="entities/research/小林.md", body="喜欢美式咖啡。"
        )
        self.assertTrue(edited.ok, edited.payload)
        read = self.call("dream_read", path="entities/research/小林.md")
        self.assertEqual(read.payload["body"], "喜欢美式咖啡。")
        self.assertEqual(read.payload["frontmatter"]["title"], "小林")
        self.assertEqual(read.payload["frontmatter"]["tags"], ["person"])

    def test_custom_root_and_directory_markdown_are_first_class_archives(self) -> None:
        self.assertTrue(self.call("dream_mkdir", path="./notes/research").ok)
        self.assertTrue(self.call("dream_create", path="notes/research/a.md", body="Nested custom").ok)
        self.assertTrue(self.call("dream_create", path="overview.md", body="Root custom").ok)
        self.assertEqual(self.call("dream_list").payload["count"], 2)
        self.assertEqual(self.call("dream_read", path="overview").payload["body"], "Root custom")
        hits = self.call("dream_search", query="Nested custom").payload["hits"]
        self.assertIn("notes/research/a.md", {item["path"] for item in hits})
        index = self.store.read(INDEX_NAME).body
        self.assertIn("[[notes/research/a|a]]", index)
        self.assertIn("[[overview|overview]]", index)
        self.assertTrue(self.call("dream_edit", path="overview.md", body="Root edited").ok)
        self.assertTrue(self.call("dream_delete", path="notes", recursive=True).ok)
        self.assertFalse((self.store.root / "notes").exists())
        self.assertNotIn("notes/research", self.store.read(INDEX_NAME).body)

    def test_bare_markdown_name_keeps_existing_entity_resolution(self) -> None:
        self.assertTrue(self.call("dream_create", path="entities/foo.md", body="Entity").ok)
        self.assertEqual(self.call("dream_read", path="foo.md").payload["path"], "entities/foo.md")
        self.assertTrue(self.call("dream_edit", path="foo.md", body="Edited entity").ok)
        self.assertEqual(self.store.read("entities/foo.md").body, "Edited entity")
        self.assertFalse((self.store.root / "foo.md").exists())

    def test_ambiguous_bare_name_never_deletes_either_archive(self) -> None:
        self.store.root.mkdir(parents=True, exist_ok=True)
        (self.store.root / "same.md").write_text("root\n", encoding="utf-8")
        self.store.write_file("entities/same.md", "entity\n")
        for tool in ("dream_read", "dream_delete"):
            with self.subTest(tool=tool):
                result = self.call(tool, path="same.md")
                self.assertFalse(result.ok)
                self.assertIn("same.md", result.payload["error"])
        self.assertTrue((self.store.root / "same.md").is_file())
        self.assertTrue((self.store.root / "entities/same.md").is_file())
        # 显式 ./ 始终指根级；带目录的路径始终指 canonical，两种都可安全操作。
        self.assertEqual(self.call("dream_read", path="./same.md").payload["body"].strip(), "root")
        # 带目录的删除是明确的；移除 canonical 后，根级同名档案也可由裸名字读取。
        self.assertTrue(self.call("dream_delete", path="entities/same.md").ok)
        self.assertEqual(self.call("dream_read", path="same.md").payload["body"].strip(), "root")

    def test_create_does_not_introduce_root_canonical_name_collision(self) -> None:
        self.assertTrue(self.call("dream_create", path="unique.md", body="Root").ok)
        result = self.call("dream_create", path="entities/unique.md", body="Entity")
        self.assertFalse(result.ok)
        self.assertFalse((self.store.root / "entities/unique.md").exists())
        self.assertEqual(self.store.read("unique.md").body, "Root")

    def test_create_and_update_are_not_interchangeable(self) -> None:
        self.assertTrue(self.call("dream_create", path="entities/a.md", body="one").ok)
        duplicate = self.call("dream_create", path="entities/a.md", body="two")
        self.assertFalse(duplicate.ok)
        self.assertEqual(self.store.read("entities/a.md").body, "one")
        missing = self.call("dream_edit", path="entities/missing.md", body="two")
        self.assertFalse(missing.ok)
        self.assertFalse((self.store.root / "entities/missing.md").exists())

    def test_observations_and_legacy_readme_are_editable_but_readme_cannot_be_deleted(self) -> None:
        self.assertTrue(self.call("dream_create", path=OBSERVATIONS_NAME, body="观察").ok)
        self.assertTrue(self.call("dream_edit", path=OBSERVATIONS_NAME, body="新观察").ok)
        self.assertTrue(self.call("dream_create", path=README_NAME, body="总览").ok)
        self.assertTrue(self.call("dream_edit", path=README_NAME, body="新总览").ok)
        self.assertEqual(self.store.read(README_NAME).body, "新总览")
        deleted = self.call("dream_delete", path=README_NAME)
        self.assertFalse(deleted.ok)
        self.assertTrue((self.store.root / README_NAME).is_file())

    def test_legacy_protected_files_and_traversal_never_change_disk(self) -> None:
        protected = (
            INDEX_NAME, LOG_NAME, MEMORY_INSTRUCTION_NAME, SECTIONS_CONFIG,
            "./" + INDEX_NAME, "./" + MEMORY_INSTRUCTION_NAME,
        )
        for path in protected:
            for tool in ("dream_create", "dream_edit", "dream_delete"):
                with self.subTest(tool=tool, path=path):
                    result = self.call(tool, path=path, body="bad")
                    self.assertFalse(result.ok)
        for path in ("../outside.md", "/tmp/outside.md", "C:/outside.md", "raw/source_1/a.md"):
            with self.subTest(path=path):
                self.assertFalse(self.call("dream_create", path=path, body="bad").ok)
        self.assertFalse((self.root / "outside.md").exists())

    def test_non_boolean_recursive_is_rejected(self) -> None:
        result = self.call("dream_delete", path="entities/x", recursive="false")
        self.assertFalse(result.ok)
        self.assertIn("recursive", result.payload["error"])


class SessionVaultNativeTests(SessionVaultCase):
    def setUp(self) -> None:
        super().setUp()
        self.store.init_skeleton()

    def test_native_create_regenerates_manifest_readme_and_index(self) -> None:
        result = self.call(
            "dream_create", path="sections/projects/alpha.md", body="Alpha project body"
        )
        self.assertTrue(result.ok, result.payload)
        self.assertIn("sections/projects/alpha.md", self.store.manifest().paths())
        self.assertIn("Alpha project body", self.store.read(README_NAME).body)
        self.assertIn("sections/projects/alpha", self.store.read(INDEX_NAME).body)
        # 嵌套文件已是合法档案，不应被结构校验误报为坏文件名。
        self.assertFalse(
            any("projects/alpha" in item and "路径" in item for item in self.store.check()["problems"])
        )

    def test_custom_section_delete_prunes_manifest_and_regenerates(self) -> None:
        self.assertTrue(self.call("dream_create", path="sections/custom.md", body="custom").ok)
        self.assertIn("sections/custom.md", self.store.manifest().paths())
        deleted = self.call("dream_delete", path="sections/custom.md")
        self.assertTrue(deleted.ok, deleted.payload)
        self.assertFalse((self.store.root / "sections/custom.md").exists())
        self.assertNotIn("sections/custom.md", self.store.manifest().paths())
        self.assertNotIn("[[sections/custom|", self.store.read(INDEX_NAME).body)

    def test_native_generated_log_memory_archive_and_raw_are_read_only(self) -> None:
        protected = (
            README_NAME,
            INDEX_NAME,
            SECTIONS_CONFIG,
            LOG_NAME,
            MEMORY_INSTRUCTION_NAME,
            "sections/earlier_months.md",
            "sections/archived_month/2026-10/this_month.md",
            "raw/source_1/a.md",
            "./" + MEMORY_INSTRUCTION_NAME,
            "./sections/archived_month/2026-10/this_month.md",
        )
        for path in protected:
            for tool in ("dream_create", "dream_edit", "dream_delete"):
                with self.subTest(tool=tool, path=path):
                    self.assertFalse(self.call(tool, path=path, body="bad").ok)

    def test_native_observations_are_editable_and_listed_in_index(self) -> None:
        result = self.call("dream_create", path=OBSERVATIONS_NAME, body="Observation")
        self.assertTrue(result.ok, result.payload)
        self.assertEqual(self.store.read(OBSERVATIONS_NAME).body, "Observation")
        self.assertIn("[[observations|Observations]]", self.store.read(INDEX_NAME).body)

    def test_builtin_section_can_be_edited_but_not_deleted(self) -> None:
        path = "sections/personal_context.md"
        self.assertTrue(self.call("dream_create", path=path, body="Context").ok)
        self.assertTrue(self.call("dream_edit", path=path, body="New context").ok)
        result = self.call("dream_delete", path=path)
        self.assertFalse(result.ok)
        self.assertEqual(self.store.read(path).body, "New context")

    def test_case_variants_cannot_bypass_native_protection(self) -> None:
        fixed = "sections/personal_context.md"
        archived = "sections/archived_month/2026-10/this_month.md"
        self.store.write_file(fixed, "fixed original")
        self.store.write_file(archived, "archive original")
        for path in (fixed.upper(), "sections/PERSONAL_CONTEXT.md"):
            with self.subTest(path=path):
                self.assertFalse(self.call("dream_delete", path=path).ok)
        edited = self.call(
            "dream_edit", path="sections/ARCHIVED_MONTH/2026-10/this_month.md", body="bad"
        )
        self.assertFalse(edited.ok)
        self.assertEqual(self.store.read(fixed).body.strip(), "fixed original")
        self.assertEqual(self.store.read(archived).body.strip(), "archive original")

    def test_internal_link_cannot_alias_a_protected_archive(self) -> None:
        archived = "sections/archived_month/2026-10/this_month.md"
        self.store.write_file(archived, "archive original")
        link = self.store.root / "entities/alias"
        if not _make_dir_link(link, self.store.root / "sections/archived_month"):
            self.skipTest("这台机器不能创建目录链接/junction")
        result = self.call(
            "dream_edit", path="entities/alias/2026-10/this_month.md", body="bad"
        )
        self.assertFalse(result.ok)
        self.assertEqual(self.store.read(archived).body.strip(), "archive original")

    def test_generated_files_can_exceed_the_single_note_limit(self) -> None:
        first = self.call("dream_create", path="sections/a.md", body="甲" * 7000)
        second = self.call("dream_create", path="sections/b.md", body="乙" * 7000)
        self.assertTrue(first.ok, first.payload)
        self.assertTrue(second.ok, second.payload)
        readme = (self.store.root / README_NAME).read_text(encoding="utf-8")
        self.assertGreater(len(readme), self.store.write_max_chars)
        self.assertIn("甲" * 100, readme)
        self.assertIn("乙" * 100, readme)
        self.assertIn("sections/a.md", self.store.manifest().paths())
        self.assertIn("sections/b.md", self.store.manifest().paths())


class SessionVaultDirectoryTests(SessionVaultCase):
    def test_mkdir_empty_delete_and_recursive_delete(self) -> None:
        created = self.call("dream_mkdir", path="entities/research/ai/")
        self.assertTrue(created.ok, created.payload)
        self.assertEqual(created.payload["action"], "created")
        self.assertTrue((self.store.root / "entities/research/ai").is_dir())
        existing = self.call("dream_mkdir", path="entities/research/ai")
        self.assertEqual(existing.payload["action"], "existing")

        self.assertTrue(self.call("dream_create", path="entities/research/ai/a.md", body="A").ok)
        refused = self.call("dream_delete", path="entities/research")
        self.assertFalse(refused.ok)
        self.assertIn("recursive=true", refused.payload["error"])
        self.assertTrue((self.store.root / "entities/research/ai/a.md").is_file())

        deleted = self.call("dream_delete", path="entities/research", recursive=True)
        self.assertTrue(deleted.ok, deleted.payload)
        self.assertEqual(deleted.payload["files"], 1)
        self.assertFalse((self.store.root / "entities/research").exists())
        self.assertNotIn("entities/research", self.store.read(INDEX_NAME).body)

    def test_recursive_delete_preflights_non_markdown_without_partial_deletion(self) -> None:
        self.assertTrue(self.call("dream_create", path="entities/group/a.md", body="A").ok)
        other = self.store.root / "entities/group/notes.txt"
        other.write_text("not an archive", encoding="utf-8")
        result = self.call("dream_delete", path="entities/group", recursive=True)
        self.assertFalse(result.ok)
        self.assertTrue((self.store.root / "entities/group/a.md").is_file())
        self.assertTrue(other.is_file())

    def test_top_level_archive_and_raw_directories_are_protected(self) -> None:
        for path in ("sections", "entities", "sections/archived_month", "raw"):
            with self.subTest(path=path):
                self.assertFalse(self.call("dream_delete", path=path, recursive=True).ok)
        for path in ("sections/archived_month/x", "raw/source_1"):
            with self.subTest(path=path):
                self.assertFalse(self.call("dream_mkdir", path=path).ok)

    def test_exact_top_directory_wins_over_same_named_file(self) -> None:
        self.assertTrue(self.call("dream_create", path="entities/entities.md", body="keep").ok)
        result = self.call("dream_delete", path="entities", recursive=True)
        self.assertFalse(result.ok)
        self.assertEqual(self.store.read("entities/entities.md").body, "keep")

    def test_outside_junction_is_rejected_without_deleting_anything(self) -> None:
        self.assertTrue(self.call("dream_create", path="entities/group/a.md", body="A").ok)
        outside = self.root / "outside"
        outside.mkdir()
        victim = outside / "victim.md"
        victim.write_text("outside original", encoding="utf-8")
        link = self.store.root / "entities/group/junction"
        if not _make_dir_link(link, outside):
            self.skipTest("这台机器不能创建目录链接/junction")
        result = self.call("dream_delete", path="entities/group", recursive=True)
        self.assertFalse(result.ok)
        self.assertEqual(victim.read_text(encoding="utf-8"), "outside original")
        self.assertTrue((self.store.root / "entities/group/a.md").is_file())

    def test_missing_markdown_file_reports_missing_not_bad_directory(self) -> None:
        result = self.call("dream_delete", path="entities/missing.md")
        self.assertFalse(result.ok)
        self.assertIn("没有", result.payload["error"])
        self.assertNotIn("目录", result.payload["error"])

    def test_directory_names_use_the_same_windows_guards(self) -> None:
        for path in ("entities/NUL/x", "entities/a:b", "entities/bad.md", "../x", "NUL/x"):
            with self.subTest(path=path):
                self.assertFalse(self.call("dream_mkdir", path=path).ok)


class SessionVaultRuntimeTests(unittest.TestCase):
    def test_cli_turn_refreshes_readme_after_a_session_edit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            settings = test_settings(Path(raw))
            VaultStore(settings.vault_dir).init_skeleton()
            provider = FakeProvider([text_reply("done")])
            with Runtime(settings, "vault-session-edit", provider=provider, web=False) as runtime:
                result = runtime.registry.call(
                    "dream_create",
                    json.dumps({"path": "sections/custom.md", "body": "Fresh vault fact"}),
                )
                self.assertTrue(result.ok, result.payload)
                outcome = runtime.loop.run("next")
                self.assertTrue(outcome.ok)
                system = provider.calls[0]["messages"][0].content
                self.assertIn("Fresh vault fact", system)

    def test_english_tool_results_contain_no_chinese(self) -> None:
        with tempfile.TemporaryDirectory() as raw, i18n.use("en"):
            store = VaultStore(Path(raw) / "vault")
            tools = build_session_vault_edit_tools(store)
            create = tools[0].run({"path": "entities/a.md", "body": "A"})
            denied = tools[1].run({"path": MEMORY_INSTRUCTION_NAME, "body": "bad"})
            for result in (create, denied):
                self.assertFalse(CJK.search(result.display))
                self.assertFalse(CJK.search(json.dumps(result.payload, ensure_ascii=False)))
