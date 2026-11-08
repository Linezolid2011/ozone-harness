"""Dream vault（M7）：路径安全、frontmatter、README 预算、只读工具、注入位次。

这个文件里**没有一条测试需要联网或调用模型**。
其中最要紧的是 `VaultPathTests` —— 那是模型第一次能写"文件路径"，
而我们在 Windows 上，于是有一整类平台特有的坑（设备名、大小写、
非法字符、结尾点）必须先被钉死，再谈功能。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness.context import build_messages, build_system_message, vault_prompt_text
from harness.reset import preview_reset
from harness.runtime import Runtime
from harness.tools.registry import ToolRegistry
from harness.tools.vault_tools import VaultListTool, VaultReadTool, VaultSearchTool
from harness.vault import (
    README_NAME,
    Frontmatter,
    VaultPathError,
    VaultStore,
    dump_frontmatter,
    normalize_rel_path,
    parse_frontmatter,
)

from .helpers import FakeProvider, test_settings


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def store(self, **kwargs) -> VaultStore:
        return VaultStore(self.tmp / "vault", **kwargs)


# --------------------------------------------------------------------------
# 路径：该拒的必须拒，该放的必须放
# --------------------------------------------------------------------------


class VaultPathTests(TempCase):
    def test_memory_instruction_is_read_only_even_without_sections_yaml(self) -> None:
        store = self.store()
        self.assertFalse(store.is_native(), "这条专门覆盖旧 vault 模式")
        for operation in (
            lambda: store.write_file("memory_instruction.md", "# Memory instructions\n"),
            lambda: store.delete_file("memory_instruction.md"),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(VaultPathError):
                    operation()

    def test_accepts_the_three_canonical_forms(self) -> None:
        self.assertEqual(normalize_rel_path("README.md"), "README.md")
        self.assertEqual(normalize_rel_path("sections/口味.md"), "sections/口味.md")
        self.assertEqual(normalize_rel_path("entities/小林.md"), "entities/小林.md")
        self.assertEqual(
            normalize_rel_path("entities/项目-Vault.md"), "entities/项目-Vault.md"
        )

    def test_readme_case_is_folded_but_entities_are_not(self) -> None:
        # README 只有一个，Windows 上 readme.md / README.MD 本来就是同一个文件
        self.assertEqual(normalize_rel_path("readme.md"), README_NAME)
        self.assertEqual(normalize_rel_path("README.MD"), README_NAME)
        # 实体文件则要求严格小写后缀：大小写不敏感的系统上，
        # Foo.md 与 foo.md 是同一个文件，宽松会让"两个实体"互相覆盖
        with self.assertRaises(VaultPathError):
            normalize_rel_path("entities/X.MD")
        with self.assertRaises(VaultPathError):
            normalize_rel_path("Entities/x.md")

    def test_normalizes_separators_and_outer_whitespace(self) -> None:
        self.assertEqual(normalize_rel_path("entities\\小林.md"), "entities/小林.md")
        self.assertEqual(normalize_rel_path("  entities/小林.md  "), "entities/小林.md")

    def test_rejects_traversal_and_absolute_forms(self) -> None:
        for raw in (
            "../x.md",
            "entities/../x.md",
            "entities/../../x.md",
            "..",
            "/etc/passwd",
            "/entities/x.md",
            "C:/x.md",
            "C:x.md",
            "//server/share/x.md",
            "~/x.md",
            "entities//x.md",
            "entities/",
            "",
            "   ",
            "entities/a\x00b.md",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(VaultPathError):
                    normalize_rel_path(raw)

    def test_rejects_windows_specific_hazards(self) -> None:
        """设备名、非法字符、结尾点 —— Windows 上这些不是洁癖，是会静默出错。"""
        for raw in (
            "entities/NUL.md",       # 写进空设备：看起来成功，其实什么都没写
            "entities/nul.md",
            "entities/CON.md",
            "entities/PRN.md",
            "entities/AUX.md",
            "entities/COM1.md",
            "entities/LPT9.md",
            "entities/nul.x.md",     # 第一个点之前是设备名，同样是设备
            "entities/a:b.md",
            "entities/a*b.md",
            "entities/a?.md",
            "entities/a<b.md",
            "entities/a>b.md",
            "entities/a|b.md",
            'entities/a"b.md',
            "entities/a.md.",
            "entities/ x.md",
            # `x .md` 在 NTFS 上是**另一个文件**，但人眼里和 `x.md` 一模一样 ——
            # 同一个实体会被写成两份，孤儿检查也会报两次
            "entities/x .md",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(VaultPathError):
                    normalize_rel_path(raw)

    def test_inner_spaces_are_legal(self) -> None:
        """名字**内部**的空格是合法的（Windows 也允许），只有首尾不行。"""
        self.assertEqual(
            normalize_rel_path("entities/项目 Vault.md"), "entities/项目 Vault.md"
        )

    def test_rejects_wrong_extension_and_depth(self) -> None:
        for raw in (
            "entities/x.txt",
            "entities/x",
            "entities/.md",
            "entities/sub/x.md",
            "other/x.md",
            "entities",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(VaultPathError):
                    normalize_rel_path(raw)

    def test_rejects_over_long_names(self) -> None:
        with self.assertRaises(VaultPathError):
            normalize_rel_path("entities/" + "长" * 45 + ".md")
        # 刚好在上限内则放行（上限算的是整个文件名，含 `.md`）
        self.assertTrue(normalize_rel_path("entities/" + "长" * 37 + ".md"))
        with self.assertRaises(VaultPathError):
            normalize_rel_path("entities/" + "长" * 38 + ".md")

    def test_friendly_resolution_adds_the_directory(self) -> None:
        store = self.store()
        store.write_file("entities/小林.md", "---\ntitle: 小林\n---\n\n后端同事。\n")
        store.write_file("sections/口味.md", "---\ntitle: 口味\n---\n\n手冲。\n")
        self.assertEqual(store.resolve_friendly("小林"), "entities/小林.md")
        self.assertEqual(store.resolve_friendly("小林.md"), "entities/小林.md")
        self.assertEqual(store.resolve_friendly("口味"), "sections/口味.md")
        self.assertEqual(store.resolve_friendly("README"), README_NAME)
        # 带目录的写法仍然走严格校验（写错大小写要报错，而不是猜）
        with self.assertRaises(VaultPathError):
            store.resolve_friendly("Entities/小林.md")

    def test_symlink_pointing_outside_is_refused(self) -> None:
        """符号链接能让"看起来在 vault 里"的路径落到外面去。

        归一化看不出来（`entities/x.md` 完全合法），只有 resolve 之后比较真实
        前缀才发现。所以这一条必须由 `_resolve_canonical` 兜住。
        """
        outside = self.tmp / "outside"
        outside.mkdir()
        root = self.tmp / "vault"
        root.mkdir()
        link = root / "entities"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:  # Windows 需要管理员/开发者模式
            self.skipTest(f"这台机器不能创建符号链接：{exc}")

        store = VaultStore(root)
        with self.assertRaises(VaultPathError):
            store.write_file("entities/x.md", "x")
        with self.assertRaises(VaultPathError):
            store.read("entities/x.md")

    def test_path_of_stays_inside_the_vault(self) -> None:
        store = self.store()
        resolved = store.path_of("entities/小林.md")
        self.assertTrue(resolved.is_relative_to((self.tmp / "vault").resolve()))
        self.assertTrue(str(resolved).endswith("小林.md"))


# --------------------------------------------------------------------------
# frontmatter：一个刻意做小的 YAML 子集
# --------------------------------------------------------------------------


class FrontmatterTests(unittest.TestCase):
    def test_parses_inline_and_block_lists(self) -> None:
        front, body, warnings = parse_frontmatter(
            "---\n"
            "title: 小林\n"
            "kind: person\n"
            "tags: [同事, 技术]\n"
            "aliases:\n"
            "  - 小林哥\n"
            "  - 林工\n"
            "---\n"
            "\n"
            "正文 [[项目-Vault]]\n"
        )
        self.assertEqual(front.title, "小林")
        self.assertEqual(front.kind, "person")
        self.assertEqual(front.tags, ("同事", "技术"))
        self.assertEqual(front.aliases, ("小林哥", "林工"))
        self.assertEqual(warnings, [])
        self.assertIn("正文", body)

    def test_unknown_keys_are_preserved_verbatim(self) -> None:
        """用户手工加的键不能被我们吃掉。"""
        front, _, _ = parse_frontmatter("---\ntitle: x\npinned: true\n---\n正文\n")
        self.assertEqual(front.extra, {"pinned": "true"})
        self.assertIn("pinned: true", dump_frontmatter(front))
        self.assertEqual(front.to_dict()["pinned"], "true")

    def test_round_trip_is_byte_stable(self) -> None:
        original = "---\ntitle: 小林\ntags: [a, b]\n---\n\n正文\n"
        front, _, _ = parse_frontmatter(original)
        once = dump_frontmatter(front)
        again = dump_frontmatter(parse_frontmatter(once + "\n正文\n")[0])
        self.assertEqual(once, again)

    def test_values_needing_quotes_are_quoted(self) -> None:
        front = Frontmatter(title="含: 冒号", aliases=("引号\"在这里",))
        dumped = dump_frontmatter(front)
        back, _, _ = parse_frontmatter(dumped + "\n正文\n")
        self.assertEqual(back.title, "含: 冒号")
        self.assertEqual(back.aliases, ('引号"在这里',))

    def test_broken_frontmatter_degrades_instead_of_crashing(self) -> None:
        """坏 frontmatter 是**数据质量问题**，不是攻击 —— 所以降级照读 + 一条警告。

        与路径校验的 fail-closed 不矛盾：路径是"要落到磁盘哪里"的动作，
        猜错会写错地方；frontmatter 只是文件里的一段文本。
        """
        cases = [
            ("---\ntitle: x\n", "frontmatter 没有结束", "title: x"),
            ("---\n没有冒号的一行\n---\n正文\n", "无法解析的行", "正文"),
            ("---\n: 空键\n---\n正文\n", "键是空的", "正文"),
            ("---\n- 孤立的列表项\n---\n正文\n", "列表项没有对应的键", "正文"),
        ]
        for text, expected, marker in cases:
            with self.subTest(text=text):
                front, body, warnings = parse_frontmatter(text)
                self.assertTrue(any(expected in item for item in warnings), warnings)
                self.assertIn(marker, body)
                self.assertEqual(front.title, "")

    def test_file_without_frontmatter_is_all_body(self) -> None:
        front, body, warnings = parse_frontmatter("# 标题\n\n内容\n")
        self.assertEqual(front.title, "")
        self.assertEqual(warnings, [])
        self.assertIn("内容", body)


# --------------------------------------------------------------------------
# VaultStore：读、写、检索、邻域、校验
# --------------------------------------------------------------------------


class VaultStoreTests(TempCase):
    def _seed(self, store: VaultStore) -> None:
        store.write_file(
            "entities/小林.md",
            dump_frontmatter(Frontmatter(title="小林", kind="person"))
            + "\n后端同事，和 [[项目-Vault]] 一起做过事。\n",
        )
        store.write_file(
            "entities/项目-Vault.md",
            dump_frontmatter(Frontmatter(title="项目-Vault", kind="project"))
            + "\n把记忆分成两个系统。\n",
        )
        store.write_file(
            "sections/口味.md",
            dump_frontmatter(Frontmatter(title="口味")) + "\n手冲，不加糖。\n",
        )
        store.write_file(
            README_NAME,
            "# README\n\n- 同事：小林（entities/小林.md）\n- 口味：手冲\n",
        )

    def test_read_only_operations_never_create_the_directory(self) -> None:
        """界面上的"看一眼"不能改变磁盘状态（同 reset.preview 的原则）。"""
        root = self.tmp / "vault"
        store = VaultStore(root)
        self.assertFalse(store.exists())
        self.assertFalse(store.has_content())
        self.assertEqual(store.list_files(), [])
        self.assertEqual(store.readme_text(), ("", False))
        report = store.check()
        self.assertFalse(report["exists"])
        self.assertFalse(root.exists(), "只读操作竟然创建了 vault 目录")

    def test_write_then_read_round_trip(self) -> None:
        store = self.store()
        self._seed(store)
        self.assertTrue(store.has_content())
        file = store.read("entities/小林.md")
        self.assertEqual(file.path, "entities/小林.md")
        self.assertEqual(file.frontmatter.kind, "person")
        self.assertIn("后端同事", file.body)
        self.assertEqual(file.warnings, [])

    def test_write_leaves_no_temp_files(self) -> None:
        store = self.store()
        self._seed(store)
        leftovers = [path.name for path in (self.tmp / "vault").rglob(".*tmp*")]
        self.assertEqual(leftovers, [])

    def test_missing_file_raises_file_not_found(self) -> None:
        store = self.store()
        with self.assertRaises(FileNotFoundError):
            store.read("entities/不存在的人.md")

    def test_read_truncates_and_says_so(self) -> None:
        store = self.store(read_max_chars=50)
        store.write_file("entities/长文.md", "---\ntitle: 长文\n---\n\n" + "字" * 200)
        file = store.read("entities/长文.md")
        self.assertTrue(file.truncated)
        self.assertIn("已按单次读取上限截断", file.body)

    def test_write_enforces_its_limits(self) -> None:
        store = self.store(write_max_chars=100)
        with self.assertRaises(VaultPathError) as ctx:
            store.write_file("entities/大文件.md", "字" * 500)
        self.assertIn("超过单文件上限", str(ctx.exception))

        capped = self.store(max_files=2)
        capped.write_file("entities/a.md", "a")
        capped.write_file("entities/b.md", "b")
        with self.assertRaises(VaultPathError) as ctx:
            capped.write_file("entities/c.md", "c")
        self.assertIn("上限", str(ctx.exception))
        # 覆盖已有文件不受文件数上限影响
        capped.write_file("entities/a.md", "a2")

    def test_search_ranks_and_reports_snippet(self) -> None:
        store = self.store()
        self._seed(store)
        hits = store.search("后端")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["path"], "entities/小林.md")
        self.assertIn("后端", hits[0]["snippet"])
        self.assertEqual(store.search(""), [])
        self.assertEqual(store.search("完全无关的词组"), [])

    def test_neighborhood_links_backlinks_related(self) -> None:
        store = self.store()
        self._seed(store)
        store.write_file(
            "entities/老张.md",
            dump_frontmatter(Frontmatter(title="老张")) + "\n也认识 [[项目-Vault]]。\n",
        )
        mine = store.neighborhood("entities/小林.md")
        self.assertEqual([item["path"] for item in mine["links"]], ["entities/项目-Vault.md"])

        target = store.neighborhood("entities/项目-Vault.md")
        backlink_paths = {item["path"] for item in target["backlinks"]}
        self.assertIn("entities/小林.md", backlink_paths)
        self.assertIn("entities/老张.md", backlink_paths)

        # 共现：小林与老张都指向 项目-Vault
        related = {item["path"] for item in target["related"]}
        self.assertEqual(related, set())

    def test_readme_budget_truncates_on_paragraph_boundaries(self) -> None:
        store = self.store(readme_token_budget=60)
        blocks = [f"## 第{i}节\n" + "字" * 40 for i in range(6)]
        store.write_file(README_NAME, "\n\n".join(blocks))
        text, truncated = store.readme_text()
        self.assertTrue(truncated)
        self.assertIn("超出注入预算", text)
        # 段落是切分单位：留下的必须是完整的段，不能切在句子中间
        self.assertIn("## 第0节", text)
        self.assertNotIn("## 第1节", text)

    def test_readme_within_budget_is_untouched(self) -> None:
        store = self.store(readme_token_budget=1000)
        store.write_file(README_NAME, "# README\n\n很短。\n")
        text, truncated = store.readme_text()
        self.assertFalse(truncated)
        self.assertIn("很短。", text)

    def test_truncated_readme_really_is_within_budget(self) -> None:
        """截断必须**真的**把总量压到预算内 —— 否则"上限由 harness 强制"是假的。

        最容易写错的两条路径：① 第一段自己就超预算；② 只算正文、忘算那句截断提示。
        """
        from harness.llm_types import estimate_tokens

        cases = {
            "一整段超长中文": "字" * 5000,
            "一整段超长英文": "word " * 2000,
            "很多小段": "\n\n".join(f"## 第{i}节\n" + "字" * 30 for i in range(50)),
            "首段超长后面很短": "字" * 4000 + "\n\n短的。",
            "中英混排": ("mixed 内容 " * 400),
        }
        for budget in (200, 1200):
            for name, text in cases.items():
                with self.subTest(budget=budget, case=name):
                    store = self.store(readme_token_budget=budget)
                    store.write_file(README_NAME, text)
                    got, truncated = store.readme_text()
                    self.assertTrue(truncated)
                    self.assertLessEqual(
                        estimate_tokens(got), budget, f"{name} 截断后仍然超预算"
                    )
                    self.assertIn("超出注入预算", got)

    def test_check_reports_orphans_and_oversize_readme(self) -> None:
        store = self.store(readme_token_budget=20)
        store.write_file(README_NAME, "字" * 200)
        store.write_file("entities/孤儿.md", "---\ntitle: 孤儿\n---\n\n没人提到我。\n")
        report = store.check()
        joined = "\n".join(report["problems"])
        self.assertIn("孤儿档案", joined)
        self.assertIn("超过注入预算", joined)

    def test_check_reports_hand_placed_bad_names(self) -> None:
        """手工放进来的文件名不走写入路径，所以校验必须能**发现**它。"""
        store = self.store()
        store.write_file(README_NAME, "# README\n")
        entities = self.tmp / "vault" / "entities"
        entities.mkdir(parents=True, exist_ok=True)
        bad = entities / ("长" * 45 + ".md")
        bad.write_text("手工放的\n", encoding="utf-8")
        report = store.check()
        self.assertTrue(
            any("文件名不合规" in item for item in report["problems"]), report["problems"]
        )

    def test_check_is_clean_for_a_well_formed_vault(self) -> None:
        store = self.store()
        store.write_file(README_NAME, "# README\n\n- 小林（entities/小林.md）\n")
        store.write_file("entities/小林.md", "---\ntitle: 小林\n---\n\n后端同事。\n")
        report = store.check()
        self.assertEqual(report["problems"], [])
        # 口径：README 是索引，不算档案
        self.assertEqual(report["files"], 1)
        self.assertTrue(report["has_readme"])

    def test_init_skeleton_writes_no_content(self) -> None:
        store = self.store()
        created = store.init_skeleton()
        self.assertIn("entities/", created)
        self.assertIn(README_NAME, created)
        self.assertTrue((self.tmp / "vault" / "entities").is_dir())
        # 幂等：第二次什么都不建
        self.assertEqual(store.init_skeleton(), [])

    def test_name_collisions_are_detected(self) -> None:
        """只在**大小写敏感**的文件系统上才可能真的存在这一对文件。

        Windows / macOS 上 `Foo.md` 与 `foo.md` 根本存不下来（后写的直接覆盖），
        所以这条在那边跳过 —— 但检查逻辑本身仍然要有，因为用户可能从
        Linux 拷一份 vault 过来，或者用了大小写敏感的卷。
        """
        store = self.store()
        store.write_file("entities/Foo.md", "a")
        self.assertEqual(store.find_name_collisions(), [])

        entities = self.tmp / "vault" / "entities"
        other = entities / "foo.md"
        try:
            other.write_text("b", encoding="utf-8")
        except OSError as exc:  # pragma: no cover - 大小写不敏感的平台上不会走到
            self.skipTest(f"这个文件系统存不下只差大小写的两个文件：{exc}")
        if len({item.name for item in entities.iterdir()}) < 2:
            self.skipTest("这个文件系统大小写不敏感，构造不出撞名")
        collisions = store.find_name_collisions()
        self.assertTrue(collisions, "两个只差大小写的文件必须被报出来")
        self.assertIn("Foo.md", collisions[0])


# --------------------------------------------------------------------------
# 对抗性审查抓出来的问题（每一条都对应一个真实可复现的坏行为）
# --------------------------------------------------------------------------


class AdversarialReviewTests(TempCase):
    """这些是"另一个 agent 专门来攻"时抓到的，全部有实测复现。

    教训：**"看起来对"不等于对**。撞名覆盖和扫描读穿这两条，
    代码读起来都像是对的。
    """

    def test_case_only_collision_is_refused_instead_of_overwriting(self) -> None:
        """`Foo.md` 之后再写 `foo.md`：Windows 上是同一个文件，第一份会被静默销毁。

        最阴的一点：在大小写不敏感的系统上 `foo.md` 的 `is_file()` **为真**
        （那就是 Foo.md 本身），于是"新建文件"那条分支根本不执行 ——
        所以撞名检查必须无条件跑，不能只挂在新建分支上。
        """
        store = self.store()
        store.write_file("entities/Foo.md", "AAA 第一个实体")
        if not _CASE_INSENSITIVE_FS_FOR_TEST:
            self.skipTest("这个文件系统大小写敏感，两个文件可以合法并存")

        with self.assertRaises(VaultPathError) as ctx:
            store.write_file("entities/foo.md", "BBB 第二个实体")
        self.assertIn("同一个文件", str(ctx.exception))
        # 关键：第一份**没有被覆盖**
        self.assertEqual(store.read("entities/Foo.md").body.strip(), "AAA 第一个实体")
        # 大小写不同的同一个名字，覆盖自己是被允许的
        store.write_file("entities/Foo.md", "改过的内容")
        self.assertEqual(store.read("entities/Foo.md").body.strip(), "改过的内容")

    def test_scanning_does_not_read_through_links(self) -> None:
        """vault 里的链接目录指向外面时：写被拒是一半，**读也不能读穿**。

        审查实测：`dream_read` 拒绝，但 `list_files` / `dream_search` 把外面
        那个文件的正文片段放进了工具结果 —— 同一条路径两种行为。
        """
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "秘密.md").write_text(
            "---\ntitle: 秘密\n---\n\n这里是 vault 外面的秘密 SECRET-OUTSIDE-12345\n",
            encoding="utf-8",
        )
        root = self.tmp / "vault"
        root.mkdir()
        if not _make_dir_link(root / "sections", outside):
            self.skipTest("这台机器建不了目录链接/junction")

        store = VaultStore(root)
        self.assertEqual(store.list_files(), [], "扫描把 vault 外面的文件读出来了")
        self.assertFalse(store.has_content())
        self.assertEqual(store.search("SECRET-OUTSIDE-12345"), [])
        with self.assertRaises(VaultPathError):
            store.read("sections/秘密.md")

    def test_reserved_device_names_with_spaces_are_refused(self) -> None:
        """`NUL .md` 这种"点号前带空格"的写法：Windows 同样当设备。

        只比对 `split(".")[0]` 会让它漏过去，落地时才抛一个模型看不懂的 OSError。
        """
        for raw in (
            "sections/NUL .md",
            "sections/NUL  .md",
            "sections/COM1 .md",
            "sections/aux .md",
            "sections/prn .md",
            "sections/con .md",
            "sections/NUL. .md",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(VaultPathError):
                    normalize_rel_path(raw)

    def test_placeholder_readme_is_never_injected(self) -> None:
        """骨架 README 是**程序写的占位**，一旦有真档案也不能被当成内容注入。"""
        store = self.store()
        store.init_skeleton()
        self.assertEqual(store.readme_text(), ("", False))
        store.write_file("entities/小林.md", "---\ntitle: 小林\n---\n\n后端。\n")
        self.assertEqual(store.readme_text(), ("", False))
        # 于是走"没有 README"那条分支，模型被要求自己去列目录 —— 而不是
        # 读到程序写的"（还没有整理过任何对话）"以为档案是空的
        self.assertIn("没有 README", vault_prompt_text("", available=True))

    def test_read_reports_the_real_length_not_the_truncated_one(self) -> None:
        store = self.store(read_max_chars=50)
        store.write_file("entities/长文.md", "---\ntitle: 长文\n---\n\n" + "字" * 500)
        file = store.read("entities/长文.md")
        self.assertEqual(file.total_chars, 500)
        self.assertLess(file.chars, 500)
        self.assertTrue(file.truncated)

    def test_check_does_not_blame_readable_files(self) -> None:
        """档案数超过展示上限时，`check()` 不能把**完全可读**的文件报成读取失败。

        成因是拿"截断后的列表"和"没截断的名单"取差集。
        这里绕过 `write_file`（写入端有上限）直接落盘 —— 那正是
        "手工/外部放进来一批档案"这个被审查的场景。
        """
        store = self.store(max_files=2)
        _place(store, "entities/甲0.md", "内容 0")
        _place(store, "entities/甲1.md", "内容 1")
        _place(store, "entities/甲2.md", "内容 2")
        report = store.check()
        joined = "\n".join(report["problems"])
        self.assertNotIn("读取失败", joined)
        self.assertIn("超过展示上限", joined)

    def test_entities_are_not_bulk_dropped_by_sections(self) -> None:
        """截断方向要可预测：不能"有 sections 时 entities 整批消失"。"""
        store = self.store(max_files=2)
        _place(store, "sections/一.md", "a")
        _place(store, "sections/二.md", "b")
        _place(store, "entities/丙.md", "c")
        paths = {entry.path for entry in store.list_files()}
        # 全局按路径排序后截断 → entities/丙.md 排在 sections/* 前面，所以它在
        self.assertIn("entities/丙.md", paths)

    def test_search_result_is_within_budget(self) -> None:
        """检索也要自己按预算收敛（原来只有列表和读取做了，与模块注释矛盾）。"""
        for index in range(30):
            self.store().write_file(
                f"entities/实体{index:02d}.md",
                f"---\ntitle: 实体{index}\n---\n\n共同关键词 zzz 的第 {index} 条。\n",
            )
        tool = VaultSearchTool(VaultStore(self.tmp / "vault"), max_result_chars=2000)
        payload = tool.run({"query": "共同关键词 zzz"}).payload
        self.assertGreater(payload["count"], 0)
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertLessEqual(len(rendered), 2000, rendered[:200])
        if payload.get("more"):
            self.assertEqual(
                payload["count"], len(payload["hits"]) + payload["more"]
            )

    def test_search_rejects_a_non_integer_limit(self) -> None:
        tool = VaultSearchTool(VaultStore(self.tmp / "vault"))
        result = tool.run({"query": "x", "limit": "abc"})
        self.assertFalse(result.ok)
        self.assertIn("整数", result.payload["error"])

    def test_extension_case_is_normalized_when_resolving(self) -> None:
        """`Foo.MD` 在 Windows 上就是 `Foo.md`：不该拼成 `Foo.MD.md`。"""
        store = self.store()
        store.write_file("entities/Foo.md", "内容")
        self.assertEqual(store.resolve_friendly("Foo.MD"), "entities/Foo.md")
        self.assertEqual(store.resolve_friendly("FOO.MD"), "entities/Foo.md")

    def test_newlines_in_frontmatter_values_round_trip(self) -> None:
        front = Frontmatter(title="第一行\n第二行", aliases=("反斜杠\\n写在这里",))
        dumped = dump_frontmatter(front)
        self.assertNotIn("\n第二行", dumped)
        back, _, warnings = parse_frontmatter(dumped + "\n正文\n")
        self.assertEqual(back.title, "第一行\n第二行")
        self.assertEqual(warnings, [])
        self.assertEqual(dump_frontmatter(back), dumped)

    def test_write_reports_a_busy_target_in_plain_words(self) -> None:
        """README 可能在另一个线程里正被读：写失败要给**人话**，并且不留下半截文件。

        （重试能救的是"短暂重叠"；下面这种**全程占着**的窗口救不了，
        所以这里断言的是"错误可读 + 旧内容完好 + 之后能正常写"。）
        """
        store = self.store()
        store.write_file(README_NAME, "# README\n\n第一版\n")
        if os.name != "nt":
            self.skipTest("这个平台替换被打开的文件不会失败")
        with open(store.path_of(README_NAME), "r", encoding="utf-8"):
            with self.assertRaises(VaultPathError) as ctx:
                store.write_file(README_NAME, "# README\n\n第二版\n")
        self.assertIn("占用", str(ctx.exception))
        self.assertIn("第一版", store.readme_text()[0])
        store.write_file(README_NAME, "# README\n\n第三版\n")
        self.assertIn("第三版", store.readme_text()[0])


def _place(store: VaultStore, rel: str, text: str) -> None:
    """绕过 `write_file` 直接把文件放到磁盘上（模拟"手工/外部放进来的档案"）。"""
    path = store.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


def _make_dir_link(link: Path, target: Path) -> bool:
    """尽力造一个目录链接：先试符号链接，Windows 上退回 junction（不需要管理员）。"""
    try:
        link.symlink_to(target, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name != "nt":
        return False
    try:
        completed = subprocess.run(  # noqa: S603 - 固定参数，没有外部输入
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and link.exists()


_CASE_INSENSITIVE_FS_FOR_TEST = os.name == "nt" or sys.platform == "darwin"


# --------------------------------------------------------------------------
# 只读工具
# --------------------------------------------------------------------------


class VaultToolTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = VaultStore(self.tmp / "vault")
        self.registry = ToolRegistry(max_result_chars=2000)
        for tool in (
            VaultListTool(self.store),
            VaultReadTool(self.store),
            VaultSearchTool(self.store),
        ):
            self.registry.register(tool)

    def test_tools_are_registered_and_read_only(self) -> None:
        self.assertEqual(
            self.registry.names(), ["dream_list", "dream_read", "dream_search"]
        )
        # 只有读工具：写 vault 的能力不给对话模型（ADR-002 的边界没动）
        self.assertNotIn("mem_forget", self.registry.names())

    def test_list_on_empty_vault_says_so(self) -> None:
        result = self.registry.call("dream_list", "{}")
        self.assertTrue(result.ok)
        self.assertEqual(result.payload["count"], 0)
        self.assertIn("还没有", result.payload["note"])

    def test_list_caps_and_reports_the_remainder(self) -> None:
        for index in range(30):
            self.store.write_file(f"entities/实体{index:02d}.md", f"内容 {index}")
        payload = self.registry.call("dream_list", "{}").payload
        self.assertEqual(payload["count"], 30)
        self.assertLess(len(payload["files"]), 30)
        self.assertEqual(payload["more"], 30 - len(payload["files"]))
        # 关键：整个结果必须真的放得下，而不是被 registry 事后砍
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertLessEqual(len(rendered), 2000, rendered[:200])

        filtered = self.registry.call("dream_list", '{"prefix": "entities/实体1"}').payload
        self.assertEqual(filtered["count"], 10)
        self.assertEqual(len(filtered["files"]), 10)

    def test_read_resolves_bare_name_and_reports_neighbors(self) -> None:
        self.store.write_file(
            "entities/小林.md",
            dump_frontmatter(Frontmatter(title="小林", kind="person"))
            + "\n和 [[项目-Vault]] 一起做事。\n",
        )
        self.store.write_file("entities/项目-Vault.md", "---\ntitle: 项目-Vault\n---\n项目。\n")
        payload = self.registry.call("dream_read", '{"path": "小林"}').payload
        self.assertEqual(payload["path"], "entities/小林.md")
        self.assertEqual(payload["frontmatter"]["kind"], "person")
        self.assertEqual(
            [item["path"] for item in payload["neighbors"]["links"]],
            ["entities/项目-Vault.md"],
        )

    def test_read_refuses_traversal_and_missing_files(self) -> None:
        for raw, expected in (
            ("../../etc/passwd", "两层"),
            ("/etc/passwd", "绝对路径"),
            ("C:/x.md", "盘符"),
            ("entities/NUL.md", "保留设备名"),
        ):
            with self.subTest(raw=raw):
                result = self.registry.call("dream_read", json.dumps({"path": raw}))
                self.assertFalse(result.ok)
                self.assertIn(expected, result.payload["error"])

        missing = self.registry.call("dream_read", '{"path": "entities/查无此人.md"}')
        self.assertFalse(missing.ok)
        self.assertIn("没有", missing.payload["error"])

    def test_read_body_cap_is_derived_from_the_result_budget(self) -> None:
        """工具结果的真实上限是 min(自己的设置, 循环的结果上限)。

        只看自己那一个数字，就会出现"设置里写 4000、实际被 2000 砍掉"——
        联网抓取踩过这个坑，这里由工具自己先按预算收敛。
        """
        big = self.tmp / "vault"
        VaultStore(big).write_file("entities/长文.md", "字" * 8000)
        tool = VaultReadTool(VaultStore(big), max_result_chars=2000)
        result = tool.run({"path": "entities/长文.md"})
        self.assertTrue(result.ok)
        self.assertTrue(result.payload["truncated"])
        self.assertIn("工具结果上限", result.payload["note"])
        self.assertLessEqual(len(result.as_tool_content(2000)), 2000)

        # 模型显式要一个更大的上限时，也不能突破工具的预算
        asked = tool.run({"path": "entities/长文.md", "max_chars": 99999})
        self.assertLessEqual(len(asked.as_tool_content(2000)), 2000)
        self.assertTrue(asked.payload["truncated"])

    def test_read_rejects_empty_path(self) -> None:
        result = self.registry.call("dream_read", "{}")
        self.assertFalse(result.ok)
        self.assertIn("path", result.payload["error"])

    def test_search_returns_hits_and_a_hint_when_empty(self) -> None:
        self.store.write_file("entities/小林.md", "---\ntitle: 小林\n---\n\n后端同事。\n")
        hit = self.registry.call("dream_search", '{"query": "后端"}').payload
        self.assertEqual(hit["count"], 1)
        self.assertEqual(hit["hits"][0]["path"], "entities/小林.md")

        none = self.registry.call("dream_search", '{"query": "毫不相干"}').payload
        self.assertEqual(none["count"], 0)
        self.assertIn("换", none["note"])

        empty = self.registry.call("dream_search", "{}")
        self.assertFalse(empty.ok)

    def test_large_file_is_truncated_structurally(self) -> None:
        """工具结果超限时必须**保住每一个键**（结构化截断），而不是把 JSON 切一半。"""
        self.store.write_file(
            "entities/长文.md", "---\ntitle: 长文\n---\n\n" + "字" * 5000
        )
        content = self.registry.render_content(
            self.registry.call("dream_read", '{"path": "entities/长文.md"}')
        )
        self.assertLessEqual(len(content), 2000)
        parsed = json.loads(content)
        self.assertTrue(parsed["truncated"])
        self.assertIn("path", parsed)
        self.assertIn("neighbors", parsed)


# --------------------------------------------------------------------------
# 注入：位次、分层、空 vault
# --------------------------------------------------------------------------


class VaultInjectionTests(TempCase):
    def test_readme_is_injected_before_the_memory_items(self) -> None:
        """README 一天最多变一次，记忆条目每写一次就变 —— 变得慢的放前面。"""
        readme = "## 关于你\n- 后端，正在做记忆系统改造\n"
        system = build_system_message(
            test_settings(self.tmp),
            "[mem:1] (身份) 用户是中学生",
            vault_text=vault_prompt_text(readme, available=True),
        )
        content = system.content
        self.assertLess(
            content.index("# 背景档案"),
            content.index("# 记忆条目"),
            "README 必须排在记忆条目之前，否则它一变就会让记忆块与历史全部失去缓存",
        )
        self.assertIn("后端，正在做记忆系统改造", content)
        self.assertIn("dream_list", content)

    def test_two_channels_are_explained_with_an_explicit_trust_order(self) -> None:
        # 提示词的文本现在按语言放在 `harness/prompts/{zh,en}.py`（ADR-085）。
        # 这一条断言的是**中文那一套**的原句，所以直接取 zh 的模块。
        from harness.prompts.zh import SYSTEM_PROMPT

        self.assertIn("可信度最高", SYSTEM_PROMPT)
        self.assertIn("一律以记忆条目为准", SYSTEM_PROMPT)
        # 文件系统的能力边界不能被提示词说漏
        self.assertIn("没有任何文件、系统或命令能力", SYSTEM_PROMPT)

    def test_empty_vault_says_so_and_promises_nothing(self) -> None:
        text = vault_prompt_text("", available=False)
        self.assertIn("没有可查阅的背景档案", text)
        self.assertNotIn("dream_read", text)

    def test_missing_readme_with_content_tells_the_model_to_list(self) -> None:
        text = vault_prompt_text("", available=True)
        self.assertIn("没有 README", text)
        self.assertIn("dream_list", text)

    def test_system_message_is_byte_stable_for_the_same_inputs(self) -> None:
        settings = test_settings(self.tmp)
        readme = "## 口味\n- 手冲\n"
        first = build_system_message(
            settings, "[mem:1] x", vault_text=vault_prompt_text(readme, available=True)
        )
        second = build_system_message(
            settings, "[mem:1] x", vault_text=vault_prompt_text(readme, available=True)
        )
        self.assertEqual(first.content, second.content)

    def test_volatile_time_stays_at_the_tail_even_with_a_vault(self) -> None:
        """README 进了 system，但**时间仍然只能在最后一条消息里**（ADR-044）。"""
        from datetime import datetime, timedelta, timezone

        settings = test_settings(self.tmp)
        readme = "## 关于你\n- 后端\n"
        zone = timezone(timedelta(hours=8))
        messages, _ = build_messages(
            settings,
            [],
            "[mem:1] x",
            vault_text=vault_prompt_text(readme, available=True),
            now=datetime(2026, 10, 11, 22, 0, tzinfo=zone),
        )
        self.assertIn("2026-10-11 22:00", messages[-1].content)
        self.assertNotIn("2026-10-11 22:00", messages[0].content)

    def test_build_messages_carries_the_readme_into_the_system_message(self) -> None:
        settings = test_settings(self.tmp)
        messages, _ = build_messages(
            settings,
            [],
            "(当前没有任何长期记忆)",
            vault_text=vault_prompt_text("## 本月\n- 在做记忆改造\n", available=True),
        )
        self.assertIn("在做记忆改造", messages[0].content)


# --------------------------------------------------------------------------
# 装配：Runtime 只在有档案时接入，且绝不偷偷建目录
# --------------------------------------------------------------------------


class VaultRuntimeTests(TempCase):
    def _runtime(self) -> Runtime:
        settings = test_settings(self.tmp)
        return Runtime(settings, "vault-test", provider=FakeProvider(), web=False)

    def test_empty_vault_registers_no_extra_tools_and_creates_nothing(self) -> None:
        with self._runtime() as runtime:
            self.assertEqual(
                runtime.registry.names(), ["mem_create", "mem_del", "mem_edit", "time"]
            )
            self.assertEqual(runtime.vault_text, "")
        self.assertFalse(
            (self.tmp / "vault").exists(), "空 vault 不该被 Runtime 创建出来"
        )

    def test_init_skeleton_does_not_activate_the_vault(self) -> None:
        """骨架只有一份占位 README —— 那不算"有档案"，不该注册工具、也不该注入。

        判据是"有没有真档案"，不是"README 文件在不在"。
        """
        store = self.store()
        store.init_skeleton()
        self.assertFalse(store.has_content())
        with self._runtime() as runtime:
            self.assertEqual(
                runtime.registry.names(), ["mem_create", "mem_del", "mem_edit", "time"]
            )
            self.assertEqual(runtime.vault_text, "")

    def test_content_makes_the_tools_and_the_readme_appear(self) -> None:
        from harness.vault import Frontmatter, dump_frontmatter

        VaultStore(self.tmp / "vault").write_file(
            README_NAME, "# README\n\n- 口味：手冲（sections/口味.md）\n"
        )
        VaultStore(self.tmp / "vault").write_file(
            "sections/口味.md",
            dump_frontmatter(Frontmatter(title="口味")) + "\n手冲，不加糖。\n",
        )
        with self._runtime() as runtime:
            self.assertEqual(
                runtime.registry.names(),
                [
                    "dream_list",
                    "dream_read",
                    "dream_search",
                    "mem_create",
                    "mem_del",
                    "mem_edit",
                    "time",
                ],
            )
            self.assertIn("手冲", runtime.vault_text)
            self.assertIn("手冲", runtime.loop.vault_text)

    def test_tool_schemas_are_byte_stable_across_constructions(self) -> None:
        """工具数组是请求前缀的一部分 —— 每次重建都不同就等于每轮都失效缓存。"""
        store = VaultStore(self.tmp / "vault")
        store.write_file(README_NAME, "# README\n\n- 口味：手冲（sections/口味.md）\n")
        store.write_file("sections/口味.md", "---\ntitle: 口味\n---\n\n手冲。\n")
        with self._runtime() as first:
            specs_a = json.dumps(first.registry.specs(), ensure_ascii=False, sort_keys=True)
        with self._runtime() as second:
            specs_b = json.dumps(second.registry.specs(), ensure_ascii=False, sort_keys=True)
        self.assertEqual(specs_a, specs_b)
        self.assertIn("dream_read", specs_a)


# --------------------------------------------------------------------------
# 恢复出厂设置：不许偷偷删档案
# --------------------------------------------------------------------------


class VaultResetTests(TempCase):
    def test_preview_counts_vault_files_without_creating_anything(self) -> None:
        settings = test_settings(self.tmp)
        before = preview_reset(settings)
        self.assertEqual(before["vault_files"], 0)
        self.assertFalse((self.tmp / "vault").exists())

        store = VaultStore(self.tmp / "vault")
        store.write_file(README_NAME, "# README\n")
        store.write_file("entities/小林.md", "---\ntitle: 小林\n---\n\n后端。\n")

        after = preview_reset(settings)
        # 口径与 check()["files"] 一致：README 是索引，不算档案
        self.assertEqual(after["vault_files"], 1)
        self.assertGreater(after["bytes"], before["bytes"])


if __name__ == "__main__":
    unittest.main()
