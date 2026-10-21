"""提示词可编辑（ADR-088，契约 `docs/14-editable-prompts.md`）。

这一层的价值全在**四条硬要求**上，所以测试也按它们分：

1. 改了就真的生效（不是只改了界面）；
2. **改中文不影响英文**（逐字节比对另一门）；
3. 每条能复原、整门能复原、**恢复出厂能把两门都复原**；
4. 校验挡住的是"发出去才会炸"的那一类，放行的是"用户明知后果"的那一类。

全局状态（`prompts.configure`）是**进程级**的，所以每个用例都要在 `setUp` 里指向
自己的临时目录、并在 `tearDown` 里退回 `None` —— 否则上一条用例的覆盖会漏到下一条。
"""

from __future__ import annotations

import ast
import json
import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from harness import i18n, prompts
from harness.config import ROOT
from harness.context import build_system_message, build_tail_message
from harness.dream import build_dream_system_message
from harness.prompts import PromptError
from harness.prompts import en as prompts_en
from harness.prompts import zh as prompts_zh
from harness.reset import perform_reset, preview_reset

from .helpers import test_settings


class EditTestCase(unittest.TestCase):
    """公共脚手架：一个临时数据目录 + 进程级配置 + 用完退回。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = test_settings(Path(self._tmp.name) / "data")
        self.addCleanup(prompts.configure, None)
        i18n.apply_settings(self.settings)
        self.addCleanup(i18n.set_language, "zh")

    @property
    def file(self) -> Path:
        return self.settings.data_dir / prompts.OVERRIDES_FILE

    def raw(self) -> dict:
        return json.loads(self.file.read_text(encoding="utf-8"))

    def system_text(self) -> str:
        return build_system_message(self.settings, "（记忆占位）").content


class TakesEffectTests(EditTestCase):
    """改了就真的生效 —— 一条覆盖若只改界面不改行为，这个功能就是假的。"""

    def test_system_prompt_override_reaches_the_request(self) -> None:
        self.assertIn("你是运行在 O₃ᶻᵒⁿᵉ", self.system_text())
        prompts.save("zh", "SYSTEM_PROMPT", "【我自己的】{web_tools_text}{vault_text}{memory_text}")
        text = self.system_text()
        self.assertTrue(text.startswith("【我自己的】"))
        self.assertIn("（记忆占位）", text, "占位符仍然要被填上")
        self.assertNotIn("你是运行在 O₃ᶻᵒⁿᵉ", text)

    def test_dream_prompt_override_reaches_the_consolidation_agent(self) -> None:
        before = build_dream_system_message(self.settings).content
        self.assertIn("记忆巩固器", before)
        prompts.save("zh", "DREAM_SYSTEM_PROMPT", "【整理者的新规矩】{readme_budget}")
        after = build_dream_system_message(self.settings).content
        self.assertTrue(after.startswith("【整理者的新规矩】"))
        self.assertIn("1200", after, "占位符 readme_budget 仍要被填上")

    def test_tail_prompt_override_reaches_the_last_message(self) -> None:
        now = datetime(2026, 10, 16, 12, 0)
        before = build_tail_message(now=now).content
        self.assertIn("当前时间", before)
        prompts.save("zh", "TAIL_NOW", "# 现在几点：{stamp}")
        after = build_tail_message(now=now).content
        self.assertIn("现在几点", after)

    def test_english_override_only_shows_in_english(self) -> None:
        prompts.save("en", "SYSTEM_PROMPT", "[[EN]] {web_tools_text}{vault_text}{memory_text}")
        with i18n.use("en"):
            self.assertTrue(build_system_message(self.settings, "m").content.startswith("[[EN]]"))
        with i18n.use("zh"):
            self.assertIn("你是运行在 O₃ᶻᵒⁿᵉ", self.system_text())


class LanguageIsolationTests(EditTestCase):
    """**改中文不影响英文**（用户明确要求）。"""

    def test_saving_chinese_leaves_english_byte_identical(self) -> None:
        prompts.save("en", "SYSTEM_PROMPT", "[[EN]] {web_tools_text}{vault_text}{memory_text}")
        before = json.dumps(self.raw()["en"], ensure_ascii=False, sort_keys=True)
        prompts.save("zh", "SYSTEM_PROMPT", "【中】{web_tools_text}{vault_text}{memory_text}")
        prompts.save("zh", "DREAM_INSTRUCTION", "开始吧")
        self.assertEqual(
            json.dumps(self.raw()["en"], ensure_ascii=False, sort_keys=True), before
        )
        self.assertEqual(prompts.state()["overrides"]["en"], ["SYSTEM_PROMPT"])

    def test_english_override_survives_a_chinese_revert(self) -> None:
        prompts.save("zh", "SYSTEM_PROMPT", "【中】{web_tools_text}{vault_text}{memory_text}")
        prompts.save("en", "SYSTEM_PROMPT", "[[EN]] {web_tools_text}{vault_text}{memory_text}")
        prompts.revert("zh")
        self.assertEqual(prompts.state()["overrides"]["zh"], [])
        self.assertEqual(prompts.state()["overrides"]["en"], ["SYSTEM_PROMPT"])
        self.assertTrue(prompts.bundle("en").SYSTEM_PROMPT.startswith("[[EN]]"))

    def test_saving_english_leaves_chinese_byte_identical(self) -> None:
        prompts.save("zh", "SYSTEM_PROMPT", "【中】{web_tools_text}{vault_text}{memory_text}")
        before = json.dumps(self.raw()["zh"], ensure_ascii=False, sort_keys=True)
        prompts.save("en", "TAIL_NOW", "# Now: {stamp}")
        self.assertEqual(
            json.dumps(self.raw()["zh"], ensure_ascii=False, sort_keys=True), before
        )

    def test_both_languages_get_their_own_text(self) -> None:
        prompts.save("zh", "TAIL_NOW", "# 中文时间：{stamp}")
        prompts.save("en", "TAIL_NOW", "# English time: {stamp}")
        self.assertTrue(prompts.bundle("zh").TAIL_NOW.startswith("# 中文时间"))
        self.assertTrue(prompts.bundle("en").TAIL_NOW.startswith("# English time"))
        # 内置那两个常量**一个字节都没动**（它们是默认值，也是守卫的对象）
        self.assertEqual(prompts_zh.TAIL_NOW, "# 当前时间\n{stamp}（{weekday}，本地时区 UTC{offset}）")
        self.assertTrue(prompts_en.TAIL_NOW.startswith("# Current time"))


class ValidationTests(EditTestCase):
    """拒绝的四种 + 警告的一种 + "与内置相同"那一种。"""

    def test_unknown_language_is_rejected(self) -> None:
        with self.assertRaises(PromptError) as ctx:
            prompts.save("jp", "SYSTEM_PROMPT", "x")
        self.assertIn("不认识的语言", str(ctx.exception))

    def test_unknown_id_is_rejected(self) -> None:
        with self.assertRaises(PromptError) as ctx:
            prompts.save("zh", "NOT_A_PROMPT", "x")
        self.assertIn("没有这条提示词", str(ctx.exception))

    def test_empty_text_is_rejected_with_a_way_out(self) -> None:
        with self.assertRaises(PromptError) as ctx:
            prompts.save("zh", "SYSTEM_PROMPT", "   \n  ")
        self.assertIn("复原", str(ctx.exception))

    def test_too_long_is_rejected(self) -> None:
        with self.assertRaises(PromptError) as ctx:
            prompts.save("zh", "SYSTEM_PROMPT", "字" * 30, max_chars=20)
        self.assertIn("太长了", str(ctx.exception))

    def test_broken_format_string_is_rejected(self) -> None:
        with self.assertRaises(PromptError) as ctx:
            prompts.save("zh", "SYSTEM_PROMPT", "只有一个花括号 {")
        self.assertIn("不是合法的模板", str(ctx.exception))

    def test_unknown_placeholder_is_rejected(self) -> None:
        """这一类最危险：保存时不报错，**发请求时**才 KeyError。"""
        with self.assertRaises(PromptError) as ctx:
            prompts.save(
                "zh", "SYSTEM_PROMPT", "{foo} {web_tools_text}{vault_text}{memory_text}"
            )
        self.assertIn("{foo}", str(ctx.exception))

    def test_dropping_a_placeholder_saves_with_a_warning(self) -> None:
        result = prompts.save("zh", "SYSTEM_PROMPT", "只要联网那段：{web_tools_text}")
        self.assertTrue(result["stored"])
        self.assertTrue(result["warnings"], "少了 memory_text 必须警告")
        self.assertIn("{memory_text}", result["warnings"][0])
        # 而且真的没被填进去
        self.assertNotIn("（记忆占位）", self.system_text())

    def test_identical_text_is_not_stored_and_clears_an_old_override(self) -> None:
        prompts.save("zh", "SYSTEM_PROMPT", "【旧】{web_tools_text}{vault_text}{memory_text}")
        result = prompts.save("zh", "SYSTEM_PROMPT", prompts_zh.SYSTEM_PROMPT)
        self.assertFalse(result["stored"])
        self.assertFalse(result["overridden"])
        self.assertFalse(self.file.exists(), "全空时文件应当被删掉")
        self.assertIn("你是运行在 O₃ᶻᵒⁿᵉ", self.system_text())

    def test_whitespace_only_difference_counts_as_identical(self) -> None:
        result = prompts.save("zh", "SYSTEM_PROMPT", prompts_zh.SYSTEM_PROMPT + "\n\n")
        self.assertFalse(result["stored"])

    def test_inspect_does_not_write_anything(self) -> None:
        prompts.inspect("zh", "SYSTEM_PROMPT", "【看看】{web_tools_text}")
        self.assertFalse(self.file.exists(), "inspect 是纯校验，不许写盘")


class RevertTests(EditTestCase):
    def test_revert_one_leaves_the_others(self) -> None:
        prompts.save("zh", "SYSTEM_PROMPT", "【A】{web_tools_text}{vault_text}{memory_text}")
        prompts.save("zh", "TAIL_NOW", "# B {stamp}")
        result = prompts.revert("zh", "SYSTEM_PROMPT")
        self.assertEqual(result["cleared"], 1)
        self.assertEqual(prompts.state()["overrides"]["zh"], ["TAIL_NOW"])
        self.assertIn("你是运行在 O₃ᶻᵒⁿᵉ", self.system_text())

    def test_revert_all_of_a_language(self) -> None:
        prompts.save("zh", "SYSTEM_PROMPT", "【A】{web_tools_text}{vault_text}{memory_text}")
        prompts.save("zh", "TAIL_NOW", "# B {stamp}")
        result = prompts.revert("zh")
        self.assertEqual(result["cleared"], 2)
        self.assertEqual(prompts.state()["overrides"]["zh"], [])
        self.assertFalse(self.file.exists())

    def test_reverting_something_not_overridden_is_idempotent(self) -> None:
        result = prompts.revert("zh", "SYSTEM_PROMPT")
        self.assertEqual(result["cleared"], 0)
        self.assertFalse(self.file.exists(), "没东西可删就不该建文件")

    def test_reverting_when_the_file_exists_but_that_key_is_not_in_it(self) -> None:
        prompts.save("zh", "TAIL_NOW", "# B {stamp}")
        stamp = self.file.stat().st_mtime_ns
        result = prompts.revert("zh", "SYSTEM_PROMPT")
        self.assertEqual(result["cleared"], 0)
        self.assertEqual(self.file.stat().st_mtime_ns, stamp, "没改动就不该碰文件")

    def test_unknown_id_on_revert_is_rejected(self) -> None:
        with self.assertRaises(PromptError):
            prompts.revert("zh", "NOPE")


class PersistenceTests(EditTestCase):
    def test_hand_editing_the_file_is_picked_up(self) -> None:
        """用户可能直接改 `data/prompts.json` —— 那也要立刻生效。"""
        prompts.save("zh", "TAIL_NOW", "# 第一次 {stamp}")
        self.assertTrue(prompts.bundle().TAIL_NOW.startswith("# 第一次"))
        payload = self.raw()
        payload["zh"]["TAIL_NOW"] = "# 手工改的 {stamp}（更长一点，确保 size 也变了）"
        self.file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.assertTrue(prompts.bundle().TAIL_NOW.startswith("# 手工改的"))

    def test_unknown_keys_and_languages_are_ignored(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "zh": {"TAIL_NOW": "# 好 {stamp}", "NOT_A_PROMPT": "忽略我", "LANG": "zh"},
                    "jp": {"TAIL_NOW": "无视"},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        bundle = prompts.bundle("zh")
        self.assertTrue(bundle.TAIL_NOW.startswith("# 好"))
        self.assertFalse(hasattr(bundle, "NOT_A_PROMPT"))
        self.assertEqual(bundle.LANG, "zh", "LANG 不能被覆盖（它不是提示词）")
        self.assertEqual(prompts.state()["error"], "")

    def test_a_corrupt_file_falls_back_to_the_builtins_loudly(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text("{ 这不是 JSON", encoding="utf-8")
        self.assertIn("你是运行在 O₃ᶻᵒⁿᵉ", self.system_text())
        error = prompts.state()["error"]
        self.assertTrue(error, "坏文件必须如实报出来，不能静默")
        self.assertIn("内置", error)

    def test_a_wrong_shape_file_falls_back_too(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps({"zh": "不是对象"}), encoding="utf-8")
        self.assertIn("你是运行在 O₃ᶻᵒⁿᵉ", self.system_text())
        self.assertTrue(prompts.state()["error"])

    def test_nothing_is_created_until_something_is_saved(self) -> None:
        prompts.bundle()
        prompts.state()
        self.assertFalse(self.file.exists())
        self.assertFalse(self.settings.data_dir.exists(), "只读地看一眼不该建目录")


class ResetTests(EditTestCase):
    def test_factory_reset_restores_both_languages(self) -> None:
        prompts.save("zh", "SYSTEM_PROMPT", "【中】{web_tools_text}{vault_text}{memory_text}")
        prompts.save("en", "SYSTEM_PROMPT", "[[EN]] {web_tools_text}{vault_text}{memory_text}")
        self.assertEqual(preview_reset(self.settings)["prompt_overrides"], 2)
        perform_reset(self.settings)
        self.assertFalse(self.file.exists())
        i18n.apply_settings(self.settings)  # 重置之后重新装一次（同真实流程）
        self.assertTrue(prompts.bundle("zh").SYSTEM_PROMPT.startswith("你是运行在 O₃ᶻᵒⁿᵉ"))
        self.assertTrue(prompts.bundle("en").SYSTEM_PROMPT.startswith("You are"))
        self.assertEqual(preview_reset(self.settings)["prompt_overrides"], 0)

    def test_dream_format_does_not_touch_prompts(self) -> None:
        """`--dream-format` 只管 dream 层（档案/运行记录/进度），与提示词无关。"""
        from harness.dream import format_dream
        from harness.memory import open_memory

        prompts.save("zh", "SYSTEM_PROMPT", "【中】{web_tools_text}{vault_text}{memory_text}")
        memory = open_memory(self.settings)
        try:
            format_dream(self.settings, memory)
        finally:
            memory.close()
        self.assertTrue(self.file.is_file(), "格式化 dream 不该动 prompts.json")
        self.assertEqual(prompts.state()["overrides"]["zh"], ["SYSTEM_PROMPT"])

    def test_reverting_one_language_does_not_need_a_reset(self) -> None:
        prompts.save("zh", "SYSTEM_PROMPT", "【中】{web_tools_text}{vault_text}{memory_text}")
        prompts.revert("zh", "SYSTEM_PROMPT")
        self.assertIn("你是运行在 O₃ᶻᵒⁿᵉ", self.system_text())


class MessageLanguageTests(EditTestCase):
    """保存/复原的**报错与警告也跟着界面语言**。

    这几句话是后端回给界面的（界面按"显示后端原话"处理），所以它们必须双语 ——
    否则英文界面上会跳出一句中文的失败原因。这处漏翻是子 agent 在截图核对时
    发现的（它当时正确地没有在前端二次翻译）。
    """

    CJK = re.compile(r"[\u4e00-\u9fff]")

    def _assert_english(self, text: str) -> None:
        self.assertTrue(text.strip())
        self.assertFalse(self.CJK.search(text), f"英文界面下出现了中文：{text}")

    def test_rejections_are_english_in_english_mode(self) -> None:
        with i18n.use("en"):
            for language, name, text, extra in (
                ("jp", "SYSTEM_PROMPT", "x", {}),
                ("en", "NOPE", "x", {}),
                ("en", "SYSTEM_PROMPT", "   ", {}),
                ("en", "SYSTEM_PROMPT", "x" * 50, {"max_chars": 10}),
                ("en", "SYSTEM_PROMPT", "{foo}", {}),
                ("en", "SYSTEM_PROMPT", "括号不配对 {", {}),
            ):
                with self.subTest(language=language, name=name, size=len(text)):
                    with self.assertRaises(PromptError) as ctx:
                        prompts.save(language, name, text, **extra)
                    self._assert_english(str(ctx.exception))

    def test_warnings_are_english_in_english_mode(self) -> None:
        with i18n.use("en"):
            result = prompts.save("en", "SYSTEM_PROMPT", "just the web part: {web_tools_text}")
            self.assertTrue(result["warnings"])
            for warning in result["warnings"]:
                self._assert_english(warning)

    def test_store_errors_are_english_in_english_mode(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text("{ 坏文件", encoding="utf-8")
        with i18n.use("en"):
            error = prompts.state()["error"]
        self._assert_english(error)

    def test_chinese_mode_keeps_the_chinese_wording(self) -> None:
        """中文界面下这几句**逐字不变**（既有测试断言的就是这些字）。"""
        with self.assertRaises(PromptError) as ctx:
            prompts.save("jp", "SYSTEM_PROMPT", "x")
        self.assertIn("不认识的语言", str(ctx.exception))


class WiringGuardTests(unittest.TestCase):
    """守卫：**应用代码不许直接调 `i18n.bundle()`**。

    那条路径拿到的是语言模块（不叠加用户覆盖），走了它就等于**静默忽略**
    用户的改动 —— 而且不会有任何报错。所以这里用 AST 扫源码（不是 grep：
    注释与 docstring 里提到它是正常的）。
    """

    ALLOWED = {"harness/i18n.py", "harness/prompts/__init__.py"}

    def test_no_module_bypasses_the_override_layer(self) -> None:
        offenders: list[str] = []
        for path in sorted((ROOT / "harness").rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel in self.ALLOWED:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr == "bundle"
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "i18n"
                ):
                    offenders.append(f"{rel}:{node.lineno} 直接调了 i18n.bundle()")
                if isinstance(node, ast.ImportFrom) and node.module == "i18n":
                    for alias in node.names:
                        if alias.name == "bundle":
                            offenders.append(f"{rel}:{node.lineno} from i18n import bundle")
        self.assertEqual(
            offenders,
            [],
            "这些地方绕过了用户覆盖（应当用 harness.prompts.bundle）：\n  "
            + "\n  ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
