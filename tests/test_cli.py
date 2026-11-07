"""CLI 冒烟测试：**把不花钱的命令都真跑一遍**。

来历：`--dream-format` 第一版在 `cli.py` 里用了 `display_path` 却没 import，
于是它一跑就 `NameError` —— 而**没有任何测试覆盖 CLI 这一层**：
单测直接调 `format_dream()`、Web 测试打接口，只有我手动跑才炸。
这和 ADR-066 记的 `summarize_pending` 是同一个坑：**没人走的那条路才是会坏的那条**。

所以这里不测"某个功能对不对"（那些在各自的测试文件里），只测
**"这些命令还跑得起来吗"** —— 便宜、而且正好盖住 import/拼写这类错。
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from harness import cli
from harness.events import EventLog
from harness.memory import open_memory
from harness.providers.store import ProviderProfile, ProviderStore
from harness.session_ids import validate_session_id
from harness.vault import VaultStore

from .helpers import test_settings


class CliSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.settings = test_settings(self.tmp)

        # 造一点数据，好让那些"要有东西才显示"的命令走完正常分支
        log = EventLog(self.settings.sessions_dir / "s1.jsonl", "s1")
        log.append("session_start", provider="deepseek", model="deepseek-flash")
        log.append("user_message", text="你好")
        log.append("assistant_message", message={"content": "嗯"})
        log.close()
        memory = open_memory(self.settings)
        self.addCleanup(memory.close)
        memory.create("用户在做记忆系统", "项目")
        memory.advance_cursor("s1", 1, "dream-x")
        vault = VaultStore(self.settings.vault_dir)
        vault.write_file("entities/小林.md", "---\ntitle: 小林\n---\n\n内容\n")
        vault.write_file("README.md", "# 背景档案\n\n- 小林\n")

    def run_cli(self, func, *args, **kwargs) -> tuple[int, str]:
        """跑一个命令，捕获它打印的东西（那里面就藏着 NameError 的现场）。"""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = func(*args, **kwargs)
        return code, buffer.getvalue()

    def test_read_only_commands_do_not_crash(self) -> None:
        """一路跑过去：任何一个 NameError/拼错的属性都会在这里现形。"""
        cases = {
            "--vault": lambda: cli.cmd_vault(self.settings),
            "--vault-show": lambda: cli.cmd_vault_show(self.settings, "entities/小林.md"),
            "--dreams": lambda: cli.cmd_dreams(self.settings),
            "--dream-review": lambda: cli.cmd_dream_review(self.settings, "dream-没有这个"),
            "--dream-undo（已取消）": lambda: cli.cmd_dream_undo(self.settings, "dream-x"),
            "--dream-dry-run（已取消）": cli.cmd_dream_dry_run_gone,
        }
        for label, func in cases.items():
            with self.subTest(command=label):
                code, output = self.run_cli(func)
                self.assertIn(code, (0, 1), f"{label} 返回了奇怪的状态码")
                self.assertNotIn("NameError", output, f"{label} 里有个没定义的变量：{output[-300:]}")
                self.assertNotIn("Traceback", output, f"{label} 抛异常了：{output[-300:]}")

    def test_vault_init_creates_the_skeleton(self) -> None:
        code, output = self.run_cli(cli.cmd_vault_init, self.settings)
        self.assertEqual(code, 0, output[-300:])
        self.assertTrue((self.settings.vault_dir / "sections").is_dir())

    def test_dream_format_cancels_on_the_wrong_word(self) -> None:
        """交互式：打错就**什么都没动**（这是这个动作的减速带）。"""
        from harness.reset import dream_state

        before = dream_state(self.settings)
        with mock.patch("builtins.input", return_value="no"):
            code, output = self.run_cli(cli.cmd_dream_format, self.settings)
        self.assertEqual(code, 1)
        self.assertIn("已取消", output)
        self.assertEqual(dream_state(self.settings), before, "被取消时不该动任何东西")

    def test_dream_format_runs_with_the_word(self) -> None:
        from harness.reset import dream_state

        with mock.patch("builtins.input", return_value="格式化"):
            code, output = self.run_cli(cli.cmd_dream_format, self.settings)
        self.assertEqual(code, 0, output[-300:])
        self.assertIn("已格式化并初始化", output)
        self.assertIn("档案 1 → 0 份", output)
        after = dream_state(self.settings)
        self.assertEqual(after, {"vault_files": 0, "dream_runs": 0, "cursor_sessions": 0})
        # 对话与记忆没被动
        self.assertTrue((self.settings.sessions_dir / "s1.jsonl").is_file())
        memory = open_memory(self.settings)
        try:
            self.assertEqual(memory.stats()["active"], 1)
        finally:
            memory.close()

    def test_version_flag_prints_the_product_and_version(self) -> None:
        """`--version` 与界面用**同一个来源**（`docs/version.txt`）。

        写死版本的后果是自相矛盾：界面说 1.0.0-rc1、命令行说 0.1.0，
        而"我跑的是哪个"就再也答不出来。
        """
        from harness import PRODUCT_NAME, version

        with self.assertRaises(SystemExit) as ctx:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                cli.main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        text = out.getvalue()
        self.assertIn(PRODUCT_NAME, text)
        self.assertIn(version(), text)

    def test_dream_format_yes_skips_the_question(self) -> None:
        """`--yes` 给脚本用：不该再问（问了就会卡在等待输入上）。"""
        with mock.patch("builtins.input", side_effect=AssertionError("不该问！")):
            code, _output = self.run_cli(cli.cmd_dream_format, self.settings, assume_yes=True)
        self.assertEqual(code, 0)

    def test_every_command_in_main_is_callable(self) -> None:
        """粗略兜底：`main()` 里引用的命令函数都存在（防止改名后漏改一处）。

        不做"真跑 main()"这种重活 —— 那要造 argv 与 provider；这里只确认
        **那些名字都指得到函数**，因为"改名只改了一半"正是这类 bug 的常见形状。
        """
        import inspect

        source = inspect.getsource(cli.main)
        for name in (
            "cmd_dream",
            "cmd_dreams",
            "cmd_dream_review",
            "cmd_dream_undo",
            "cmd_dream_format",
            "cmd_dream_dry_run_gone",
            "cmd_vault",
            "cmd_vault_show",
            "cmd_vault_init",
        ):
            with self.subTest(name=name):
                self.assertIn(name, source, f"main() 里没引用 {name}")
                self.assertTrue(callable(getattr(cli, name, None)), f"{name} 不是可调用对象")


class SessionIdValidationTests(unittest.TestCase):
    """CLI 与运行中共用 Runtime 的会话路径闸；这些测试不发送模型请求。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        # data 单独放一层，`../../victim` 的目标就逃出 data、却仍留在测试临时目录里。
        self.settings = test_settings(self.tmp / "data")
        store = ProviderStore(self.settings.data_dir / "providers.json")
        store.upsert(ProviderProfile(
            id="fake", label="Fake", base_url="http://127.0.0.1:1",
            api_key="offline-key", models=["fake-model"],
        ))
        self.args = SimpleNamespace(provider="fake", model="fake-model", effort="")

    def test_cli_rejects_traversal_before_creating_a_session_file(self) -> None:
        outside = self.tmp / "victim.jsonl"
        with self.assertRaises(ValueError):
            cli.open_runtime(self.settings, self.args, "../../victim")
        self.assertFalse(outside.is_file(), "非法 id 不能在 data/ 外写事件日志")
        self.assertFalse(self.settings.sessions_dir.exists(), "非法 id 不应创建会话目录")

    def test_valid_cli_session_id_creates_the_expected_log(self) -> None:
        runtime = cli.open_runtime(self.settings, self.args, "valid_session-1")
        self.assertIsNotNone(runtime)
        assert runtime is not None
        try:
            self.assertEqual(runtime.session_id, "valid_session-1")
            path = self.settings.sessions_dir / "valid_session-1.jsonl"
            self.assertTrue(path.is_file())
            self.assertTrue(path.resolve().is_relative_to(self.settings.data_dir.resolve()))
        finally:
            runtime.close()

    def test_runtime_reopen_rejects_traversal_without_replacing_current_log(self) -> None:
        runtime = cli.open_runtime(self.settings, self.args, "current-session")
        self.assertIsNotNone(runtime)
        assert runtime is not None
        try:
            current_log = runtime.log
            with self.assertRaises(ValueError):
                runtime.open_session("../../victim")
            self.assertIs(runtime.log, current_log, "坏 id 不该关掉或替换当前日志")
            self.assertEqual(runtime.session_id, "current-session")
            self.assertFalse((self.tmp / "victim.jsonl").exists())
        finally:
            runtime.close()

    def test_session_id_allowlist_boundaries(self) -> None:
        for valid in ("a", "A_-", "20261004-210125-a1b2", "a" * 64):
            with self.subTest(valid=valid):
                self.assertEqual(validate_session_id(valid), valid)
        for invalid in (
            "", ".", "..", "../victim", "..\\victim", "C:victim", "has space",
            "é", "a" * 65, "CON", "nul", "COM1", "LPT9",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    validate_session_id(invalid)


class AttachmentGcCliTests(unittest.TestCase):
    """`--attachments-gc` 与 `--replay` 里的图片（契约 §10 / §9）。

    这一组刻意用**手工落盘的文件**而不是上传接口：清理逻辑不依赖 Pillow，
    所以在无依赖模式下也要跑（跳过它就等于"没人走的那条路"）。
    """

    HEX = "a1b2c3d4e5f60718"
    TINY_PNG = b"\x89PNG\r\n\x1a\n" + b"not-a-real-image-but-that-does-not-matter"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.settings = test_settings(self.tmp)

    def run_cli(self, func, *args, **kwargs) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = func(*args, **kwargs)
        return code, buffer.getvalue()

    def _seed(self) -> tuple[Path, Path]:
        """一个被会话引用的文件 + 一个没人引用的孤儿文件。"""
        log = EventLog(self.settings.sessions_dir / "s1.jsonl", "s1")
        self.addCleanup(log.close)
        referenced = self.settings.data_dir / "attachments" / "s1" / f"{self.HEX}.png"
        referenced.parent.mkdir(parents=True, exist_ok=True)
        referenced.write_bytes(self.TINY_PNG)
        log.append(
            "user_message",
            text="这是什么动物",
            attachments=[
                {
                    "id": self.HEX,
                    "file": referenced.relative_to(self.settings.data_dir).as_posix(),
                    "name": "cat.png",
                }
            ],
        )
        orphan = (
            self.settings.data_dir / "attachments" / "s2" / f"{'b' * 16}.png"
        )
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(self.TINY_PNG + b"orphan")
        return referenced, orphan

    def test_gc_reports_orphans_and_deletes_nothing_by_default(self) -> None:
        referenced, orphan = self._seed()
        code, output = self.run_cli(cli.cmd_attachments_gc, self.settings)
        self.assertEqual(code, 0, output[-300:])
        self.assertIn("孤儿", output)
        self.assertIn("--yes", output)
        self.assertIn(orphan.name, output)
        # **默认只报告**：两个文件都还在
        self.assertTrue(orphan.is_file(), "没加 --yes 就不许删")
        self.assertTrue(referenced.is_file())

    def test_gc_with_yes_deletes_orphans_and_keeps_referenced_files(self) -> None:
        referenced, orphan = self._seed()
        code, output = self.run_cli(
            cli.cmd_attachments_gc, self.settings, assume_yes=True
        )
        self.assertEqual(code, 0, output[-300:])
        self.assertIn("已删除 1 个文件", output)
        self.assertFalse(orphan.is_file(), "孤儿文件该被删掉")
        # 这条要有牙齿：被会话引用的文件一个都不许删
        self.assertTrue(referenced.is_file(), "被引用的文件被误删了")
        self.assertEqual(referenced.read_bytes(), self.TINY_PNG)

    def test_gc_on_a_clean_directory_says_so(self) -> None:
        code, output = self.run_cli(cli.cmd_attachments_gc, self.settings)
        self.assertEqual(code, 0)
        self.assertIn("没有", output)

    def test_replay_shows_that_a_message_carried_an_image(self) -> None:
        """带附件的历史在 `--replay` 里必须看得出"这里有一张图"。"""
        log = EventLog(self.settings.sessions_dir / "s2.jsonl", "s2")
        self.addCleanup(log.close)
        log.append(
            "user_message",
            text="",
            attachments=[
                {"id": self.HEX, "name": "cat.png", "file": f"attachments/s2/{self.HEX}.png"}
            ],
        )
        log.append("assistant_message", message={"content": "我看到一只猫"})

        code, output = self.run_cli(
            cli.cmd_replay, self.settings, "s2", show_prompts=False
        )
        self.assertEqual(code, 0, output[-300:])
        self.assertIn("[图片 cat.png]", output)

    def test_replay_survives_a_broken_attachments_field(self) -> None:
        """手工改坏的日志（`attachments` 不是数组）不该让重放整个崩掉。"""
        log = EventLog(self.settings.sessions_dir / "s3.jsonl", "s3")
        self.addCleanup(log.close)
        log.append("user_message", text="你好", attachments=3)

        code, output = self.run_cli(
            cli.cmd_replay, self.settings, "s3", show_prompts=False
        )
        self.assertEqual(code, 0, output[-300:])
        self.assertNotIn("Traceback", output)

    def test_main_dispatches_to_the_gc_command(self) -> None:
        """改名只改一半是这类 bug 的常见形状：确认 main() 里确实引用了它。"""
        import inspect

        self.assertIn("cmd_attachments_gc", inspect.getsource(cli.main))
        self.assertTrue(callable(getattr(cli, "cmd_attachments_gc", None)))


if __name__ == "__main__":
    unittest.main()
