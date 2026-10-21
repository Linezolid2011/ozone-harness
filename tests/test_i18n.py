"""界面语言的守卫（ADR-085，契约 `docs/12-i18n.md` §6）。

这些断言**全部是机械的**，不靠人眼：
两个语言的 key 集合一致、长模板同名同占位符、以及**英文输出里一个汉字都没有**。
最后那条是"漏翻"的唯一可靠判据 —— 翻译漏了一半时，界面看起来是好的，
只有一眼扫过去才会发现夹着中文。
"""

from __future__ import annotations

import re
import string
import unittest
from pathlib import Path

from harness import i18n
from harness.context import build_messages, build_system_message, render_for_summary
from harness.llm_types import Message
from harness.prompts import bundle

from .helpers import test_settings

CJK = re.compile(r"[\u4e00-\u9fff]")

#: 英文里**允许**出现汉字的地方（每条都要能说出理由）。
#: 语言的名字用它自己的语言写，这样切错了也认得出来 —— 那是刻意的。
EN_CJK_ALLOWED = {"ui.language.name.zh"}

#: 长模板里允许出现汉字的常量（目前是空的：英文那一套应当全英文）。
EN_BUNDLE_CJK_ALLOWED: set[str] = set()


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


class CatalogParityTests(unittest.TestCase):
    def test_only_the_two_known_languages(self) -> None:
        self.assertEqual(i18n.LANGUAGES, ("zh", "en"))
        self.assertEqual(i18n.DEFAULT_LANGUAGE, "zh")

    def test_every_catalog_module_is_wired_in(self) -> None:
        """新加了一对 catalog 却忘了接进 `_CATALOG_MODULES` → 这里变红。"""
        self.assertEqual(i18n.missing_modules(), [])

    def test_both_languages_define_exactly_the_same_keys(self) -> None:
        zh, en = i18n.keys("zh"), i18n.keys("en")
        self.assertEqual(sorted(zh - en), [], "这些 key 英文里没有")
        self.assertEqual(sorted(en - zh), [], "这些 key 中文里没有")

    def test_no_key_is_defined_in_two_places(self) -> None:
        """同一个 key 两处定义时，谁生效取决于导入顺序 —— 最难查的一类文案 bug。"""
        self.assertEqual(i18n.duplicate_keys(), {})

    def test_every_key_resolves_in_both_languages(self) -> None:
        for key in sorted(i18n.keys("zh")):
            for language in i18n.LANGUAGES:
                with i18n.use(language):
                    self.assertTrue(i18n.t(key).strip(), f"{language} 的 {key} 是空的")

    def test_every_value_is_a_valid_format_string(self) -> None:
        """占位符写坏了（少一个大括号、名字里有空格）在这里就报，
        而不是等到某次请求才 `ValueError`。"""
        for language in i18n.LANGUAGES:
            for key, text in i18n.catalog(language).items():
                with self.subTest(language=language, key=key):
                    list(string.Formatter().parse(text))  # 解析不了会抛

    def test_missing_key_is_loud(self) -> None:
        """查不存在的 key 要**报错**，不许静默给一个空串。"""
        with self.assertRaises(KeyError):
            i18n.t("ui.this.key.does.not.exist")


class BundleParityTests(unittest.TestCase):
    def _public(self) -> dict[str, object]:
        from harness.prompts import en, zh

        def names(module) -> set[str]:
            return {
                name
                for name in dir(module)
                if not name.startswith("_") and name != "LANG"
            }

        self.assertEqual(sorted(names(zh) - names(en)), [], "英文缺这些常量")
        self.assertEqual(sorted(names(en) - names(zh)), [], "中文缺这些常量")
        return {"zh": zh, "en": en}

    def test_both_bundles_expose_the_same_names(self) -> None:
        self._public()

    def test_placeholders_match_one_to_one(self) -> None:
        modules = self._public()
        for name in dir(modules["zh"]):
            if name.startswith("_") or name == "LANG":
                continue
            zh_text = getattr(modules["zh"], name)
            en_text = getattr(modules["en"], name)
            if not isinstance(zh_text, str):
                continue
            with self.subTest(constant=name):
                self.assertEqual(
                    sorted(_placeholders(zh_text)), sorted(_placeholders(en_text)),
                    f"{name} 的占位符两边不一致",
                )

    def test_prompts_format_with_dummy_values(self) -> None:
        """"两边的占位符一样"还不够：得真的 format 一遍，确保没有多余/漏掉的键。"""
        modules = self._public()
        for name in dir(modules["zh"]):
            if name.startswith("_") or name == "LANG":
                continue
            text = getattr(modules["en"], name)
            if not isinstance(text, str):
                continue
            wanted = _placeholders(text)
            if not wanted:
                continue
            with self.subTest(constant=name):
                filled = text.format(**{key: "X" for key in wanted})
                self.assertNotIn("{", filled.replace("{{", "").replace("}}", ""))


class NoChineseInEnglishTests(unittest.TestCase):
    """**这条才是"漏翻"的判据**：英文模式下，模型看到的每一段文字里都不该有汉字。"""

    def test_english_catalog_has_no_cjk(self) -> None:
        offenders = []
        for key, text in i18n.catalog("en").items():
            if key in EN_CJK_ALLOWED:
                continue
            if CJK.search(text):
                offenders.append(key)
        self.assertEqual(offenders, [], f"这些英文文案里还有汉字：{offenders}")

    def test_english_bundle_has_no_cjk(self) -> None:
        module = bundle("en")
        offenders = []
        for name in dir(module):
            if name.startswith("_") or name == "LANG" or name in EN_BUNDLE_CJK_ALLOWED:
                continue
            text = getattr(module, name)
            if isinstance(text, str) and CJK.search(text):
                offenders.append(name)
        self.assertEqual(offenders, [], f"英文长模板里还有汉字：{offenders}")

    def test_english_system_message_has_no_cjk(self) -> None:
        settings = test_settings(Path(".").resolve() / "tmp-i18n-check")
        with i18n.use("en"):
            message = build_system_message(
                settings,
                "[mem:1] 用户喜欢手冲",          # 记忆**内容**可以是中文（内容不翻）
                web_tools="you can search",
                vault_text="",
            )
        # 只查"框架文字"：把注入的内容抹掉之后，剩下的部分不许有汉字
        framework = str(message.content).replace("[mem:1] 用户喜欢手冲", "")
        self.assertEqual(CJK.findall(framework), [])

    def test_english_tail_message_and_summary_rendering_have_no_cjk(self) -> None:
        settings = test_settings(Path(".").resolve() / "tmp-i18n-check")
        with i18n.use("en"):
            messages, _ = build_messages(
                settings, [Message(role="user", content="hello")], "mem"
            )
        tail = messages[-1]
        framework = str(tail.content).replace("hello", "")
        self.assertEqual(CJK.findall(framework), [])
        with i18n.use("en"):
            rendered = render_for_summary(
                [Message(role="user", content="hi"), Message(role="assistant", content="ok")],
                max_chars=None,
            )
        self.assertEqual(CJK.findall(rendered), [], f"摘要渲染里还有汉字：{rendered!r}")

    def test_english_dream_prompt_has_no_cjk(self) -> None:
        with i18n.use("en"):
            text = bundle().DREAM_SYSTEM_PROMPT.format(readme_budget=1200)
        self.assertEqual(CJK.findall(text), [])


class LanguageStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / i18n.SETTINGS_FILE

    def test_read_does_not_create_the_file(self) -> None:
        """界面上的"看一眼"不能改磁盘（同 preview_reset / MemoryLimitStore）。"""
        store = i18n.LanguageStore(self.path)
        self.assertEqual(store.language(), "zh")
        self.assertFalse(self.path.exists())

    def test_set_writes_and_reads_back(self) -> None:
        store = i18n.LanguageStore(self.path)
        change = store.set("en")
        self.assertTrue(change.ok)
        self.assertEqual(change.language, "en")
        self.assertTrue(self.path.exists())
        self.assertEqual(i18n.LanguageStore(self.path).language(), "en")
        # 只存语言这一件事
        import json

        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"language": "en"})

    def test_unknown_value_is_rejected_loudly(self) -> None:
        """设置界面那条路要**严格**：不认识的取值明确报错，而不是悄悄退回默认。
        （`normalize()` 会把不认识的一律折成 zh —— 那样"我写了 Japanese"会变成
        "切到了中文"，看起来成功、其实用户的要求被丢掉了。）"""
        store = i18n.LanguageStore(self.path)
        change = store.set("Japanese")
        self.assertFalse(change.ok)
        self.assertIn("Japanese", change.error)
        self.assertFalse(self.path.exists(), "失败时不该落盘")

    def test_aliases_are_understood(self) -> None:
        for value, expected in (
            ("en-US", "en"), ("English", "en"), ("英文", "en"),
            ("zh-CN", "zh"), ("中文", "zh"), ("ZH_hans", "zh"),
        ):
            with self.subTest(value=value):
                self.assertEqual(i18n.normalize(value), expected)

    def test_bad_seed_falls_back_instead_of_crashing(self) -> None:
        """环境变量/文件里的坏值退回默认（同 `_int_range` 的口径）。"""
        self.path.write_text('{"language": "klingon"}', encoding="utf-8")
        self.assertEqual(i18n.LanguageStore(self.path).language(), "zh")
        self.path.write_text("not json at all", encoding="utf-8")
        self.assertEqual(i18n.LanguageStore(self.path).language(), "zh")

    def test_public_state_shape(self) -> None:
        state = i18n.LanguageStore(self.path).public_state()
        self.assertEqual(state["languages"], ["zh", "en"])
        self.assertEqual(state["default"], "zh")
        self.assertEqual(state["language"], "zh")

    def test_timezone_defaults_to_utc_plus_8(self) -> None:
        store = i18n.LanguageStore(self.path)
        self.assertEqual(store.timezone(), 8.0)
        self.assertFalse(self.path.exists(), "读一眼不该建文件")

    def test_timezone_set_writes_and_reads_back(self) -> None:
        store = i18n.LanguageStore(self.path)
        result = store.set_timezone(-5)
        self.assertTrue(result["ok"])
        self.assertEqual(result["timezone"], -5.0)
        self.assertEqual(i18n.LanguageStore(self.path).timezone(), -5.0)

    def test_timezone_bad_value_is_rejected_loudly(self) -> None:
        store = i18n.LanguageStore(self.path)
        for bad in (5.5, 99, -99, "abc", True):
            with self.subTest(value=bad):
                self.assertFalse(store.set_timezone(bad)["ok"])
        self.assertFalse(self.path.exists(), "失败时不该落盘")

    def test_timezone_public_state_fields(self) -> None:
        state = i18n.LanguageStore(self.path).public_state()
        self.assertEqual(state["timezone"], 8.0)
        self.assertEqual(state["timezone_default"], 8.0)
        self.assertEqual(state["timezone_min"], -12)
        self.assertEqual(state["timezone_max"], 14)


class SwitchingTests(unittest.TestCase):
    def test_switching_changes_what_the_model_sees(self) -> None:
        settings = test_settings(Path(".").resolve() / "tmp-i18n-check")
        with i18n.use("zh"):
            chinese = str(build_system_message(settings, "mem").content)
        with i18n.use("en"):
            english = str(build_system_message(settings, "mem").content)
        self.assertNotEqual(chinese, english)
        self.assertIn("你的记忆", chinese)
        self.assertIn("Your memory", english)

    def test_switch_is_scoped_and_restores(self) -> None:
        before = i18n.current()
        with i18n.use("en"):
            self.assertEqual(i18n.current(), "en")
        self.assertEqual(i18n.current(), before)

    def test_switch_restores_even_on_exception(self) -> None:
        before = i18n.current()
        with self.assertRaises(RuntimeError):
            with i18n.use("en"):
                raise RuntimeError("boom")
        self.assertEqual(i18n.current(), before)

    def test_apply_settings_prefers_the_runtime_file(self) -> None:
        """运行期文件优先、`.env` 里的当默认（同 ADR-057 的记忆上限）。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            settings = test_settings(Path(tmp), language="zh")
            i18n.LanguageStore(Path(tmp) / i18n.SETTINGS_FILE).set("en")
            try:
                self.assertEqual(i18n.apply_settings(settings), "en")
            finally:
                i18n.set_language("zh")


if __name__ == "__main__":
    unittest.main()
