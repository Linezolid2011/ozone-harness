"""工具层双语文案的守卫（`docs/12-i18n.md`；全局守卫是 `tests/test_i18n.py`，那个不动）。

这里查的是**工具层自己的四件事**：

1. 英文模式下，模型看到的东西（每个工具的描述、每个参数的说明、每条 `MESSAGES`）
   里**一个汉字都没有** —— 那是"漏翻"唯一可靠的判据；
2. 中文模式下，描述与改造前的原句逐字一致（改造只搬了地方，一个字都没改）；
3. 两个语言的 `tool.*` key 集合完全一致，且每个工具、每个参数都有 key 兜着；
4. 改造后的代码里**不留中文字面量**（注释与 docstring 不算），描述是**构造时**现取的
   —— 类属性上还留着老描述、或者在 import 时算好，都会在这里变红。
"""

from __future__ import annotations

import ast
import json
import re
import string
import tempfile
import unittest
from pathlib import Path
from typing import Any

from harness import i18n
from harness.dream_records import ReadTracker
from harness.memory import MemoryStore
from harness.net.config import WebToolsConfig
from harness.tools import (
    dream_tools,
    memory_tools,
    text_en,
    text_zh,
    vault_tools,
    web_tools,
)
from harness.tools.dream_tools import build_dream_tools
from harness.tools.registry import ToolRegistry, ToolResult
from harness.vault import VaultStore

#: 汉字 + 中文标点（比 `tests/test_i18n.py` 只查汉字的判据更严一点：
#: 英文文案里连一个全角标点都不该有）。
CJK = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

#: 英文模式下**允许**出现汉字的地方。工具层是**空集合**（刻意的）：
#: 只有"语言自己的名字"那一类界面文案才需要用自己的语言写，工具描述没有这种必要。
EN_CJK_ALLOWED: set[str] = set()

#: 改造后不许再出现中文字面量的模块（注释与 docstring 是给人看的，不算）。
LITERAL_FREE_MODULES = (
    "registry.py",
    "memory_tools.py",
    "vault_tools.py",
    "web_tools.py",
    "dream_tools.py",
)

TOOLS_DIR = Path(__file__).resolve().parent.parent / "harness" / "tools"


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def _as_regex(template: str) -> str:
    """把一条文案模板变成"占位符处任意"的正则（共用文案的占位符在工具里已被填好）。"""
    parts: list[str] = []
    for literal, name, _spec, _conversion in string.Formatter().parse(template):
        parts.append(re.escape(literal))
        if name:
            parts.append(".*")
    return "".join(parts)


def _cjk(text: object) -> str:
    return "".join(CJK.findall(str(text)))


class _EchoTool:
    """最小可用工具：让注册表那几条错误走得通。"""

    name = "echo"

    def __init__(self) -> None:
        self.description = "echo"
        self.parameters = {"type": "object", "properties": {}}

    def run(self, args: dict[str, Any]) -> ToolResult:
        return ToolResult(ok=True, payload={"args": args}, display="ok")


class _BoomTool:
    name = "boom"

    def __init__(self) -> None:
        self.description = "boom"
        self.parameters = {"type": "object", "properties": {}}

    def run(self, args: dict[str, Any]) -> ToolResult:
        raise RuntimeError("boom")


class _ToolCase(unittest.TestCase):
    """造出**所有**工具实例（描述是构造时取的，所以实例就是"模型会看到什么"）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.memory = MemoryStore(self.tmp / "memory.db", max_items=20, max_chars=60)
        self.addCleanup(self.memory.close)
        self.vault = VaultStore(self.tmp / "vault")

    def tools(self) -> list[Any]:
        built: list[Any] = [
            memory_tools.MemCreateTool(self.memory),
            memory_tools.MemEditTool(self.memory),
            memory_tools.MemDeleteTool(self.memory),
            vault_tools.VaultListTool(self.vault),
            vault_tools.VaultReadTool(self.vault),
            vault_tools.VaultSearchTool(self.vault),
            web_tools.WebSearchTool(WebToolsConfig()),
            web_tools.WebOpenUrlTool(WebToolsConfig()),
        ]
        built.extend(
            build_dream_tools(
                store=self.vault,
                sessions_dir=self.tmp / "sessions",
                cursors={},
                tracker=ReadTracker({}),
            )
        )
        return built

    def common_values(self, language: str) -> set[str]:
        return {
            value
            for key, value in i18n.catalog(language).items()
            if key.startswith("tool.common.")
        }


# --------------------------------------------------------------------------
# catalog 本身
# --------------------------------------------------------------------------


class ToolCatalogTests(unittest.TestCase):
    def test_tool_keys_match_between_the_two_languages(self) -> None:
        zh = {key for key in text_zh.MESSAGES if key.startswith("tool.")}
        en = {key for key in text_en.MESSAGES if key.startswith("tool.")}
        self.assertEqual(sorted(zh - en), [], "这些 tool.* key 英文里没有")
        self.assertEqual(sorted(en - zh), [], "这些 tool.* key 中文里没有")

    def test_placeholders_match_one_to_one(self) -> None:
        for key in sorted(text_zh.MESSAGES):
            with self.subTest(key=key):
                self.assertEqual(
                    sorted(_placeholders(text_zh.MESSAGES[key])),
                    sorted(_placeholders(text_en.MESSAGES[key])),
                    f"{key} 的占位符两边不一致",
                )

    def test_english_catalog_has_no_chinese(self) -> None:
        offenders = sorted(
            key
            for key, value in text_en.MESSAGES.items()
            if key not in EN_CJK_ALLOWED and CJK.search(value)
        )
        self.assertEqual(offenders, [], f"这些英文文案里还有汉字：{offenders}")

    def test_chinese_catalog_values_are_not_empty(self) -> None:
        for key, value in text_zh.MESSAGES.items():
            with self.subTest(key=key):
                self.assertTrue(value.strip(), f"{key} 是空的")

    def test_every_key_resolves_in_both_languages(self) -> None:
        for language in i18n.LANGUAGES:
            with i18n.use(language):
                for key in text_zh.MESSAGES:
                    with self.subTest(language=language, key=key):
                        self.assertTrue(i18n.t(key).strip())


# --------------------------------------------------------------------------
# 工具描述与参数说明
# --------------------------------------------------------------------------


class ToolDescriptionTests(_ToolCase):
    def test_every_tool_and_parameter_is_covered_by_a_key(self) -> None:
        """每个工具 / 每个参数都得有 key 兜着（或明说用的是 `tool.common.*`）。"""
        keys = set(i18n.keys("zh")) | set(i18n.keys("en"))
        for language in i18n.LANGUAGES:
            with i18n.use(language):
                common = self.common_values(language)
                for tool in self.tools():
                    with self.subTest(language=language, tool=tool.name):
                        desc_key = f"tool.{tool.name}.desc"
                        self.assertIn(desc_key, keys, f"{tool.name} 没有描述 key")
                        self.assertEqual(tool.description, i18n.t(desc_key))
                        for param, spec in tool.parameters.get("properties", {}).items():
                            key = f"tool.{tool.name}.param.{param}"
                            described = spec.get("description", "")
                            self.assertTrue(described, f"{tool.name}.{param} 没有说明")
                            if key in keys:
                                self.assertEqual(
                                    described,
                                    i18n.t(key),
                                    f"{key} 与工具里的说明不一致",
                                )
                            else:
                                self.assertTrue(
                                    any(
                                        re.fullmatch(_as_regex(template), described)
                                        for template in common
                                    ),
                                    f"{tool.name}.{param} 用的是共用文案，但主语料里没有："
                                    f"{described!r}",
                                )

    def test_english_descriptions_and_parameters_have_no_chinese(self) -> None:
        with i18n.use("en"):
            for tool in self.tools():
                with self.subTest(tool=tool.name):
                    offenders = _cjk(tool.description)
                    self.assertEqual(offenders, "", f"{tool.name} 的描述漏翻了")
                    for param, spec in tool.parameters.get("properties", {}).items():
                        self.assertEqual(
                            _cjk(spec.get("description", "")),
                            "",
                            f"{tool.name}.{param} 的说明漏翻了",
                        )

    def test_descriptions_are_substantive_in_both_languages(self) -> None:
        """"非空"还不够：漏打 key 时描述会变成一个短词，这里拦下来。"""
        for language in i18n.LANGUAGES:
            with i18n.use(language):
                for tool in self.tools():
                    with self.subTest(language=language, tool=tool.name):
                        self.assertGreater(
                            len(tool.description), 30, f"{tool.name} 的描述太短了"
                        )
                        self.assertNotIn("{", tool.description, "占位符没有填")
                        self.assertFalse(
                            tool.description.startswith("tool."), "描述还是一个 key"
                        )

    def test_descriptions_are_read_at_construction_time(self) -> None:
        """语言是运行期可切的：类属性上钉死的描述（或 import 时算好的）在这里变红。"""
        with i18n.use("zh"):
            chinese = {tool.name: tool.description for tool in self.tools()}
        with i18n.use("en"):
            english = {tool.name: tool.description for tool in self.tools()}
        self.assertEqual(sorted(chinese), sorted(english))
        self.assertTrue(chinese, "一个工具都没造出来")
        for name, text in chinese.items():
            with self.subTest(tool=name):
                self.assertTrue(CJK.search(text), f"{name} 的中文描述里没有汉字？")
                self.assertEqual(_cjk(english[name]), "", f"{name} 的英文描述漏翻了")
                self.assertNotEqual(text, english[name])

    def test_no_tool_bakes_its_description_into_the_class(self) -> None:
        for tool in self.tools():
            with self.subTest(tool=tool.name):
                self.assertNotIn("description", vars(type(tool)))
                self.assertNotIn("parameters", vars(type(tool)))


# --------------------------------------------------------------------------
# 中文那一套必须逐字是原句
# --------------------------------------------------------------------------


class ChineseIsTheOriginalTextTests(_ToolCase):
    def test_key_descriptions_are_verbatim(self) -> None:
        with i18n.use("zh"):
            self.assertEqual(
                i18n.t("tool.mem_create.desc"),
                "创建一条长期记忆。当用户透露**稳定、长期、对未来对话有用**的事实时使用，例如："
                "身份与背景、偏好与习惯、长期目标与正在做的项目、重要的人或约定、"
                "以及用户明确要求记住的内容。"
                "不要为以下内容建立记忆：寒暄与客套、一次性的提问、临时状态、"
                "你自己刚刚给出的回答、以及对话历史里已经能直接看到的信息。"
                "每条记忆写成**一句自包含的事实陈述**（例如「用户偏好用中文交流」），"
                "不要写成「用户今天说……」。",
            )
            self.assertEqual(
                i18n.t("tool.web_open_url.open_failed_hint"),
                "这个地址打不开。**不要接着猜别的 URL** —— 猜地址基本不会对，而且很烧钱。"
                "正确做法是：换一组关键词重新 web_search，或者直接告诉用户你没查到、"
                "并说明卡在哪一步。",
            )
            self.assertEqual(
                i18n.t("tool.dream_search.no_hits"),
                "档案里没有字面匹配的内容。可以是换个关键词再试，"
                "或者用 dream_list 看看都有哪些档案。",
            )
            self.assertEqual(
                i18n.t("tool.readme_write.over_budget", tokens=1500, budget=1200),
                "README 超出预算（1500/1200 token），注入时会被截断。"
                "请把细节挪进 sections / entities，README 只留索引。",
            )
            self.assertEqual(
                i18n.t("tool.dream_read.truncated_by_budget", total=100, returned=50),
                "正文共 100 字符，本次只返回前 50 个"
                "（这是**工具结果上限**决定的，不是文件只有这么长）；"
                "要更多内容请分次读别的档案。",
            )
            self.assertEqual(i18n.t("tool.common.category_hint"), "可选分类，建议取值：身份、偏好、项目、关系、约定、其他。默认 general。")

    def test_payload_text_is_verbatim(self) -> None:
        with i18n.use("zh"):
            edit = memory_tools.MemEditTool(self.memory)
            self.assertEqual(
                edit.run({"id": "abc"}).payload["error"],
                "id 必须是整数，收到：'abc'",
            )
            self.assertEqual(
                vault_tools.VaultReadTool(self.vault).run({}).payload["error"],
                "path 不能为空",
            )
            self.assertEqual(
                web_tools.WebSearchTool(WebToolsConfig(search_backend="off"))
                .run({"query": "x"})
                .payload["error"],
                "联网搜索已被关闭。",
            )
            self.assertEqual(
                dream_tools.ReadmeWriteTool(self.vault).run({}).payload["error"],
                "markdown 不能为空",
            )

    def test_display_lines_are_verbatim(self) -> None:
        """`display` 那一行是给 CLI 的简报 —— 中文模式下仍是原来的中文。"""
        with i18n.use("zh"):
            created = memory_tools.MemCreateTool(self.memory).run({"content": "用户喜欢美式咖啡"})
            self.assertTrue(created.ok)
            self.assertIn("记住 #", created.display)
            searched = vault_tools.VaultSearchTool(self.vault).run({"query": "小林"})
            self.assertIn("档案检索「小林」命中 0 个", searched.display)


# --------------------------------------------------------------------------
# 运行时真的按当前语言取（注册表与工具回给模型的文本）
# --------------------------------------------------------------------------


class RuntimeTextTests(_ToolCase):
    def _registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(_EchoTool())
        registry.register(_BoomTool())
        return registry

    def test_registry_errors_follow_the_language(self) -> None:
        registry = self._registry()
        with i18n.use("en"):
            # `received` 是模型自己发来的原文（原样回显，不翻），所以这里喂 ASCII
            unknown = registry.call("nope", "{}")
            broken = registry.call("echo", "{oops")
            not_object = registry.call("echo", "[1, 2, 3]")
            crashed = registry.call("boom", "{}")
        for result in (unknown, broken, not_object, crashed):
            with self.subTest(result=result.payload):
                text = json.dumps(result.payload, ensure_ascii=False) + result.display
                self.assertEqual(_cjk(text), "", f"英文模式下还有汉字：{text}")

        with i18n.use("zh"):
            self.assertEqual(registry.call("nope", "{}").payload["error"], "未知工具 nope")
            self.assertIn("不是合法 JSON", registry.call("echo", "{oops").payload["error"])
            self.assertIn("必须是 JSON 对象", registry.call("echo", "[]").payload["error"])
            self.assertIn("RuntimeError", registry.call("boom", "{}").payload["error"])

    def test_tool_errors_and_displays_follow_the_language(self) -> None:
        with i18n.use("en"):
            results = {
                "mem_edit.bad_id": memory_tools.MemEditTool(self.memory).run({"id": "abc"}),
                "dream_read.empty_path": vault_tools.VaultReadTool(self.vault).run({}),
                "dream_search.empty_query": vault_tools.VaultSearchTool(self.vault).run({}),
                "web_search.disabled": web_tools.WebSearchTool(
                    WebToolsConfig(search_backend="off")
                ).run({"query": "x"}),
                "readme_write.empty": dream_tools.ReadmeWriteTool(self.vault).run({}),
                "readme_write.placeholder": dream_tools.ReadmeWriteTool(self.vault).run(
                    {"markdown": "---\nstatus: placeholder\n---\n\n# README\n"}
                ),
                "vault_write.empty_body": dream_tools.VaultWriteTool(self.vault).run(
                    {"path": "sections/a.md"}
                ),
                "vault_delete.no_reason": dream_tools.VaultDeleteTool(self.vault).run(
                    {"path": "sections/a.md"}
                ),
            }
            display = memory_tools.MemCreateTool(self.memory).run(
                {"content": "The user likes coffee"}
            ).display
        self.assertTrue(display)
        self.assertEqual(_cjk(display), "", f"英文模式的 display 漏翻了：{display}")
        for label, result in results.items():
            with self.subTest(case=label):
                text = json.dumps(result.payload, ensure_ascii=False) + result.display
                self.assertEqual(_cjk(text), "", f"{label} 的英文文案漏翻了：{text}")
                self.assertTrue(result.payload.get("error"))
                self.assertTrue(result.display)

    def test_open_failed_hint_is_lazy(self) -> None:
        """`OPEN_FAILED_HINT` 是旧名字的兼容别名：**取值时**才解析语言（PEP 562）。"""
        with i18n.use("en"):
            self.assertEqual(_cjk(web_tools.OPEN_FAILED_HINT), "")
            self.assertEqual(web_tools.OPEN_FAILED_HINT, web_tools.open_failed_hint())
        with i18n.use("zh"):
            self.assertIn("不要接着猜", web_tools.OPEN_FAILED_HINT)


# --------------------------------------------------------------------------
# 代码里不留中文字面量
# --------------------------------------------------------------------------


class _LiteralCollector(ast.NodeVisitor):
    """收集字符串常量与 f-string 模板（f-string 的插值位置折成 `{}`），跳过 docstring。"""

    def __init__(self) -> None:
        self.docstrings: set[int] = set()
        self.found: list[tuple[int, str]] = []

    def _mark(self, node: ast.AST) -> None:
        body = getattr(node, "body", [])
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            if isinstance(body[0].value.value, str):
                self.docstrings.add(id(body[0].value))

    def visit_Module(self, node: ast.Module) -> None:
        self._mark(node)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._mark(node)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._mark(node)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and id(node) not in self.docstrings:
            if CJK.search(node.value):
                self.found.append((node.lineno, node.value))

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        text = "".join(
            str(piece.value) if isinstance(piece, ast.Constant) else "{}"
            for piece in node.values
        )
        if CJK.search(text):
            self.found.append((node.lineno, text))
        # 刻意不 generic_visit：片段已经折进上面那一份模板里


class NoChineseLiteralsTests(unittest.TestCase):
    def test_tool_modules_keep_no_chinese_string_literals(self) -> None:
        """文案一律走 `i18n.t()`（契约 §3）。注释与 docstring 不受影响。"""
        for name in LITERAL_FREE_MODULES:
            collector = _LiteralCollector()
            collector.visit(ast.parse((TOOLS_DIR / name).read_text(encoding="utf-8")))
            with self.subTest(module=name):
                self.assertEqual(
                    [f"{name}:{line}: {text!r}" for line, text in collector.found],
                    [],
                    f"{name} 里还有中文字面量",
                )


if __name__ == "__main__":
    unittest.main()
