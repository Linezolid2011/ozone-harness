"""恢复出厂设置测试。

这是唯一一个破坏性且不可逆的功能，所以测试重点不是"能删掉"，而是
**删除的范围恰好正确、且没有意外的副作用**：

* 预览必须**只读** —— 不能顺手把 memory.db / providers.json 建出来；
* 只删 data_dir 里的内容，不碰旁边的 .env（那相当于出厂配置）；
* 删完目录要重建，否则程序下一次启动会到处找不到目录；
* 是真删，不是悄悄搬进某个"备份"目录（用户要的是"抹掉"）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.config import ROOT
from harness.events import EventLog
from harness.memory import MemoryStore
from harness.net.config import ToolStore
from harness.providers.store import ProviderStore
from harness.reset import (
    UnsafeDataDirError,
    assert_safe_data_dir,
    data_dir_problem,
    perform_reset,
    preview_reset,
)

from .helpers import seed_provider, test_settings


class ResetTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # data_dir 刻意是子目录：这样才能验证"数据目录之外的东西不受影响"
        self.root = Path(self._tmp.name)
        self.data_dir = self.root / "data"
        self.settings = test_settings(
            self.data_dir, api_key="sk-from-env", model="deepseek-flash"
        )

    @property
    def providers_path(self) -> Path:
        return self.data_dir / "providers.json"

    def _make_data(self) -> None:
        log = EventLog(self.settings.sessions_dir / "s1.jsonl", "s1")
        log.append("user_message", text="你好")
        log.close()

        # 一次整理运行：记录是 data/dreams/dream-*.json（不再进数据库），
        # 另外还有一份 vault 档案与它的快照
        dreams = self.settings.dreams_dir
        dreams.mkdir(parents=True, exist_ok=True)
        (dreams / "dream-20261012-100000-abcd.json").write_text(
            json.dumps({"id": "dream-20261012-100000-abcd", "started_at": "2026-10-12"},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        snapshot = dreams / "dream-20261012-100000-abcd.snapshot"
        snapshot.mkdir(parents=True, exist_ok=True)
        (snapshot / "README.md").write_text("旧的 README\n", encoding="utf-8")

        vault = self.settings.vault_dir
        (vault / "entities").mkdir(parents=True, exist_ok=True)
        (vault / "README.md").write_text("# README\n", encoding="utf-8")
        (vault / "entities" / "小林.md").write_text("后端同事\n", encoding="utf-8")

        memory = MemoryStore(self.settings.memory_db_path)
        memory.create("用户爱喝红茶", "偏好")
        memory.create("用户叫小林", "身份")
        memory.close()

        seed_provider(self.settings, base_url="https://api.deepseek.com")

    # -- 预览 --------------------------------------------------------

    def test_preview_reports_what_would_be_deleted(self) -> None:
        self._make_data()
        data = preview_reset(self.settings)
        self.assertEqual(data["sessions"], 1)
        self.assertEqual(data["dream_runs"], 1, "运行记录数")
        self.assertEqual(data["vault_files"], 1, "背景档案数（README 是索引，不算档案）")
        self.assertEqual(data["memories"], 2)
        self.assertEqual(data["providers"], 1)
        self.assertGreater(data["bytes"], 0)
        self.assertGreater(data["bytes"], 0)

    def test_preview_is_read_only(self) -> None:
        """预览绝不能创建任何文件 —— 否则"打开设置页看一眼"就改了磁盘。"""
        self.assertFalse(self.settings.data_dir.exists())

        data = preview_reset(self.settings)

        self.assertEqual(data["memories"], 0)
        self.assertEqual(data["providers"], 0)
        self.assertFalse(
            self.settings.memory_db_path.exists(), "预览不能把记忆库建出来"
        )
        self.assertFalse(self.providers_path.exists(), "预览不能把接口配置种出来")
        self.assertFalse(self.settings.data_dir.exists(), "预览不该在磁盘上留下任何痕迹")

    def test_preview_on_empty_says_zero(self) -> None:
        data = preview_reset(self.settings)
        self.assertEqual(
            [
                data[k]
                for k in ("sessions", "vault_files", "dream_runs", "memories", "providers")
            ],
            [0, 0, 0, 0, 0],
        )

    # -- 执行 --------------------------------------------------------

    def test_deletes_everything(self) -> None:
        self._make_data()
        before = preview_reset(self.settings)

        result = perform_reset(self.settings)

        self.assertEqual(result["deleted"]["sessions"], before["sessions"])
        self.assertEqual(result["deleted"]["memories"], before["memories"])
        self.assertIsNone(result["leftover"], "正常情况不该有删不掉的残留")
        after = preview_reset(self.settings)
        self.assertEqual(
            [
                after[k]
                for k in ("sessions", "vault_files", "dream_runs", "memories", "providers")
            ],
            [0, 0, 0, 0, 0],
        )

    def test_reset_formats_and_reinitializes_the_dream_layer(self) -> None:
        """**恢复出厂设置会把 dream 层格式化并初始化**（ADR-065）。

        格式化：档案、运行记录、整理进度全没；
        初始化：记忆库重建后游标表是空的，下一次整理从零开始 ——
        不会留下"档案还在、进度说整理过了"这种自相矛盾的中间状态。
        """
        from harness.events import EventLog as Log
        from harness.memory import open_memory
        from harness.reset import dream_state
        from harness.vault import VaultStore

        log = Log(self.settings.sessions_dir / "s1.jsonl", "s1")
        log.append("user_message", text="你好")
        log.append("assistant_message", message={"content": "嗯"})
        log.close()
        vault = VaultStore(self.settings.vault_dir)
        vault.write_file("sections/口味.md", "手冲")
        vault.write_file("entities/小林.md", "后端同事")
        (self.settings.dreams_dir).mkdir(parents=True, exist_ok=True)
        (self.settings.dreams_dir / "dream-20261012-100000-abcd.json").write_text(
            "{}", encoding="utf-8"
        )
        memory = open_memory(self.settings)
        memory.advance_cursor("s1", 3, "dream-x")
        memory.close()

        before = dream_state(self.settings)
        self.assertEqual(before["vault_files"], 2)
        self.assertEqual(before["dream_runs"], 1)
        self.assertEqual(before["cursor_sessions"], 1)
        preview = preview_reset(self.settings)
        self.assertEqual(preview["vault_files"], 2)
        self.assertEqual(preview["dream_runs"], 1)
        self.assertEqual(preview["cursor_sessions"], 1)

        perform_reset(self.settings)

        after = dream_state(self.settings)
        self.assertEqual(
            after, {"vault_files": 0, "dream_runs": 0, "cursor_sessions": 0}
        )
        self.assertFalse(self.settings.vault_dir.exists(), "出厂状态不该留着 vault")
        self.assertFalse(self.settings.dreams_dir.exists(), "出厂状态不该留着运行记录")
        # 初始化：完整的目录结构回来了，否则下一次启动到处找不到地方写
        self.assertTrue(self.settings.data_dir.is_dir())

    def test_reset_leaves_an_empty_vault_ready_for_the_first_run(self) -> None:
        """出厂之后 dream 是"从没整理过"的样子：没有待整理、也没有档案。

        （它**不会**替你造一个空 vault 骨架 —— 出厂状态里本来就没有那个目录，
        要骨架请用 `--vault-init`。这条断言把这个选择固定下来。）
        """
        from harness.dream_records import pending_sessions
        from harness.memory import open_memory
        from harness.vault import VaultStore

        perform_reset(self.settings)
        self.assertFalse(self.settings.vault_dir.exists())
        memory = open_memory(self.settings)
        try:
            self.assertEqual(memory.cursor_map(), {})
            self.assertEqual(pending_sessions(self.settings.sessions_dir, {}), [])
        finally:
            memory.close()
        report = VaultStore(self.settings.vault_dir).check()
        self.assertFalse(report["exists"])

    def test_data_dir_is_recreated_and_usable(self) -> None:
        """删完必须重建目录：程序后面还要往里写，不能留一个不存在的路径。"""
        self._make_data()
        perform_reset(self.settings)
        self.assertTrue(self.settings.data_dir.is_dir())

        # 立刻再写一次，应该完全正常（子目录会按需自建）
        log = EventLog(self.settings.sessions_dir / "s2.jsonl", "s2")
        log.append("user_message", text="重置之后")
        log.close()
        self.assertEqual(preview_reset(self.settings)["sessions"], 1)

    def test_wipes_the_api_keys_and_they_do_not_come_back(self) -> None:
        """用户的要求：恢复出厂设置要能连 API key 一起抹掉。

        做得到这一点，是因为 key **只存在 data/providers.json 里** ——
        没有任何"主 key"藏在 .env 里当出厂配置。所以重置之后
        一家接口都不剩，也不会自动重建。
        """
        seed_provider(self.settings, api_key="sk-secret", model="deepseek-flash")
        self.assertIn("sk-secret", self.providers_path.read_text(encoding="utf-8"))

        perform_reset(self.settings)

        store = ProviderStore(self.providers_path)
        self.assertEqual(store.providers(), [], "重置后不该剩下任何接口")
        self.assertEqual(store.defaults()["provider"], "")
        self.assertFalse(self.providers_path.exists(), "不该顺手重建 providers.json")

    def test_reset_leaves_no_key_anywhere_in_the_data_dir(self) -> None:
        """兜底：重置后整个数据目录里都不该还能找到那个 key 字符串。"""
        seed_provider(self.settings, api_key="sk-secret")
        perform_reset(self.settings)
        blob = b""
        for path in self.settings.data_dir.rglob("*"):
            if path.is_file():
                blob += path.read_bytes()
        self.assertNotIn(b"sk-secret", blob)

    def test_wipes_the_web_tool_keys_too(self) -> None:
        """联网工具的 key（Tavily）也必须被抹掉。

        它和接口的 key 一样只存在 `data/` 里，所以重置理应一起带走。
        这条是用户点名要确认的行为，得有测试钉住，不能靠"应该会"。
        """
        store = ToolStore(self.settings.data_dir / "tools.json")
        store.save_web({"search_backend": "tavily", "tavily_api_key": "tvly-secret"})
        tools_path = self.settings.data_dir / "tools.json"
        self.assertIn("tvly-secret", tools_path.read_text(encoding="utf-8"))

        perform_reset(self.settings)

        self.assertFalse(tools_path.exists(), "tools.json 应当被一起删掉")
        blob = b""
        for path in self.settings.data_dir.rglob("*"):
            if path.is_file():
                blob += path.read_bytes()
        self.assertNotIn(b"tvly-secret", blob, "数据目录里不该再有任何文件含这个 key")

    def test_reset_is_idempotent(self) -> None:
        """连按两次不能炸（第二次没什么可删）。"""
        self._make_data()
        perform_reset(self.settings)
        result = perform_reset(self.settings)
        self.assertEqual(result["deleted"]["sessions"], 0)
        self.assertTrue(self.settings.data_dir.is_dir())

    def test_reset_without_existing_data_dir(self) -> None:
        self.assertFalse(self.settings.data_dir.exists())
        result = perform_reset(self.settings)
        self.assertTrue(self.settings.data_dir.is_dir())
        self.assertEqual(result["deleted"]["sessions"], 0)

    def test_does_not_touch_files_outside_data_dir(self) -> None:
        """只有 data_dir 里的东西会被删 —— 旁边的 .env 之类必须留在原地。"""
        env_file = self.root / ".env"
        env_file.write_text("DEEPSEEK_API_KEY=sk-from-env\n", encoding="utf-8")
        self._make_data()

        perform_reset(self.settings)

        self.assertTrue(env_file.is_file(), ".env 相当于出厂配置，不能被抹掉")
        self.assertEqual(
            env_file.read_text(encoding="utf-8"), "DEEPSEEK_API_KEY=sk-from-env\n"
        )

    def test_deleted_files_are_really_gone_not_moved(self) -> None:
        """重置是把数据删掉，不是悄悄搬到一个"备份"目录里 ——
        用户要的是"抹掉"，留一份隐藏副本会变成隐私问题。"""
        self._make_data()
        perform_reset(self.settings)
        leftovers = sorted(item.name for item in self.root.iterdir())
        self.assertEqual(leftovers, ["data"], f"数据目录之外不该留下东西：{leftovers}")
        self.assertEqual(list(self.data_dir.iterdir()), [], "重建的数据目录应当是空的")


class SafeDataDirTests(unittest.TestCase):
    """动手之前的守卫（安全缺陷 3）：`data_dir` 不合法就**绝不删任何东西**。

    这是"不可逆操作的最后一道闸"。`HARNESS_DATA_DIR` 只差一个字符就能变成
    项目根（`.`）、家目录或盘根 —— 那一下会连源码和 `.env` 里的真 key 一起删掉，
    没有撤销。所以测试的重点不是"能删掉"，而是"危险路径下一个字节都不许动"。

    真拿项目根去跑一遍 `perform_reset()` 来验证，本身就是一场事故，所以这里分两半：

    * `data_dir_problem()` 是纯函数 —— 项目根 / 家目录 / 盘根可以直接喂给它；
    * `perform_reset()` 只喂一个"伪项目目录"（临时目录里造出 harness/ 和 .env），
      证明它会抛错、并且现场原封不动。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_rejects_project_root_and_its_ancestors(self) -> None:
        self.assertIsNotNone(data_dir_problem(ROOT), "项目根绝不能被当成数据目录")
        self.assertIsNotNone(data_dir_problem(ROOT.parent), "项目根的上级同样危险")

    def test_rejects_user_home(self) -> None:
        self.assertIsNotNone(data_dir_problem(Path.home()))

    def test_rejects_filesystem_root(self) -> None:
        # 当前盘符的根（C:\ 或 D:\）。它也是 with_name() 会抛 ValueError 的那个输入。
        self.assertIsNotNone(data_dir_problem(Path(self.root.anchor)))

    def test_rejects_directories_that_contain_sources_or_env(self) -> None:
        fake_project = self.root / "fake-project"
        (fake_project / "harness").mkdir(parents=True)
        self.assertIsNotNone(data_dir_problem(fake_project), "里面有 harness/ 的不能删")

        env_only = self.root / "env-only"
        env_only.mkdir()
        (env_only / ".env").write_text("KEY=sk-real\n", encoding="utf-8")
        self.assertIsNotNone(data_dir_problem(env_only), "里面有 .env 的不能删")

    def test_accepts_a_plain_data_directory(self) -> None:
        """别把守卫做得太狠：正常的（甚至还不存在的）数据目录要放行。"""
        self.assertIsNone(data_dir_problem(self.root / "data"))
        self.assertIsNone(data_dir_problem(self.root / "data-m2"))

    def test_rejects_a_path_that_is_a_file(self) -> None:
        target = self.root / "not-a-dir.txt"
        target.write_text("x", encoding="utf-8")
        self.assertIsNotNone(data_dir_problem(target))

    def test_assert_safe_data_dir_is_the_single_gate(self) -> None:
        """守卫要么静默通过、要么抛 UnsafeDataDirError，方便调用方统一接住。"""
        assert_safe_data_dir(self.root / "data")
        with self.assertRaises(UnsafeDataDirError):
            assert_safe_data_dir(ROOT)

    def test_perform_reset_refuses_a_dangerous_dir_and_deletes_nothing(self) -> None:
        """危险路径下 perform_reset 抛错，并且目录**原封不动**。

        用"伪项目目录"而不是真项目根：就算守卫哪天被改坏了，
        这个测试最多毁掉一个临时目录，不会毁掉源码。
        """
        fake_project = self.root / "fake-project"
        (fake_project / "harness").mkdir(parents=True)
        (fake_project / ".env").write_text("DEEPSEEK_API_KEY=sk-real\n", encoding="utf-8")
        victim = fake_project / "sessions" / "s1.jsonl"
        victim.parent.mkdir()
        original = '{"kind":"user_message","text":"真实对话"}\n'
        victim.write_text(original, encoding="utf-8")

        settings = test_settings(fake_project)
        with self.assertRaises(UnsafeDataDirError) as caught:
            perform_reset(settings)

        self.assertIn("拒绝", str(caught.exception), "错误信息要说人话")
        self.assertEqual(victim.read_text(encoding="utf-8"), original, "一个字节都不能变")
        self.assertTrue((fake_project / ".env").is_file(), ".env 里的 key 必须还在")
        self.assertTrue((fake_project / "harness").is_dir(), "源码目录必须还在")
        # 不能留下半截的 `<名字>.deleting-<时间戳>` 目录（那是"删到一半"的痕迹）
        self.assertEqual([item.name for item in self.root.iterdir()], ["fake-project"])

    def test_preview_still_works_on_a_dangerous_dir(self) -> None:
        """预览是只读的：即使 data_dir 危险，也该能算出"本来会删掉什么"。

        守的是**破坏性动作**，不是"看一眼" —— 否则用户连哪里配置错了都看不到。
        """
        fake_project = self.root / "fake-project"
        (fake_project / "harness").mkdir(parents=True)
        (fake_project / ".env").write_text("KEY=sk-real\n", encoding="utf-8")
        victim = fake_project / "sessions" / "s1.jsonl"
        victim.parent.mkdir()
        victim.write_text('{"kind":"user_message"}\n', encoding="utf-8")

        data = preview_reset(test_settings(fake_project))

        self.assertEqual(data["sessions"], 1)
        self.assertTrue(victim.is_file(), "预览不能删东西")
        self.assertTrue((fake_project / ".env").is_file())
