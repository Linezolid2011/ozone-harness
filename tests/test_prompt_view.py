"""「设置 → 提示词」那一页的后端：内容完整性、状态语义、零落盘、双语。

这一页的价值全在**照实**两个字上：它说"我们跟模型说了这些、模型手里有这些工具"，
那就必须与真正发出去的那一份一致。所以这里的测试分三类：

1. **一个不少**（22 条提示词 / 16 个工具）—— 分组表与 prompts 模块严格互补，
   少一条就抛异常（这条比"断言列表相等"更强：它防的是**以后**新增提示词时漏展示）；
2. **不许说谎** —— 对话侧工具的 `description` / `parameters` 必须与 `Runtime`
   真正注册的那一份逐字相同；
3. **零落盘** —— 在全新数据目录上跑一遍，"点开看一眼"不许在 `data/` 里留下任何东西。
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from harness import i18n, prompt_view
from harness.config import Settings
from harness.dream_records import ReadTracker
from harness.prompts import en as prompts_en
from harness.prompts import zh as prompts_zh
from harness.prompt_view import PROMPT_GROUPS, PROMPT_SKIP, describe_prompts
from harness.runtime import Runtime
from harness.tools.dream_tools import build_dream_tools
from harness.tools.memory_tools import MemCreateTool, MemDeleteTool, MemEditTool
from harness.vault import README_NAME, VaultStore

from .helpers import FakeProvider, test_settings

CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
VALID_STATES = {"on", "off", "session"}

# 全部工具名（写死在这里是**故意**的：页面上少一个就要报警，
# 而不是"跟着代码一起变"）。分两组，与契约 §2.3 一致。
CHAT_TOOL_NAMES = {
    "mem_create",
    "mem_edit",
    "mem_del",
    "time",
    "dream_list",
    "dream_read",
    "dream_search",
    "web_search",
    "web_open_url",
}
DREAM_TOOL_NAMES = {
    "records_list",
    "records_read",
    "vault_outline",
    "vault_read",
    "vault_write",
    "vault_delete",
    "readme_write",
}


class PromptCompletenessTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = test_settings(Path(self._tmp.name))

    def test_every_prompt_constant_is_shown_exactly_once(self) -> None:
        data = describe_prompts(self.settings)
        shown = [item["id"] for group in data["groups"] for item in group["items"]]
        expected = {
            name for name in dir(prompts_zh) if name.isupper()
        } - PROMPT_SKIP
        self.assertEqual(len(shown), len(set(shown)), "有重复展示的提示词")
        self.assertEqual(set(shown), expected)
        self.assertEqual(len(shown), 22, "提示词条数变了：要么补文档，要么更新这条断言")

    def test_prompt_text_is_the_real_thing_not_a_copy(self) -> None:
        """页面上的原文必须**逐字**等于 prompts 模块里那一份。"""
        data = describe_prompts(self.settings)
        for group in data["groups"]:
            for item in group["items"]:
                with self.subTest(item=item["id"]):
                    self.assertEqual(item["text"], str(getattr(prompts_zh, item["id"])))
                    self.assertEqual(item["chars"], len(item["text"]))

    def test_groups_cover_the_module_exactly(self) -> None:
        """分组表漏一条就要当场炸 —— 而不是页面上静默少一条。"""
        grouped = [name for _gid, names in PROMPT_GROUPS for name in names]
        public = {name for name in dir(prompts_zh) if name.isupper()} - PROMPT_SKIP
        self.assertEqual(sorted(grouped), sorted(public), "分组表与 prompts 模块对不上")

    def test_a_missing_group_member_is_loud(self) -> None:
        """变异验证：把分组表挖掉一条，`describe_prompts` 必须抛（而不是少显示一条）。"""
        original = prompt_view.PROMPT_GROUPS
        broken = (("system", tuple(original[0][1][1:])),) + tuple(original[1:])
        prompt_view.PROMPT_GROUPS = broken
        self.addCleanup(setattr, prompt_view, "PROMPT_GROUPS", original)
        with self.assertRaises(RuntimeError) as ctx:
            describe_prompts(self.settings)
        self.assertIn("SYSTEM_PROMPT", str(ctx.exception))

    def test_every_tool_is_shown_once_with_a_valid_state(self) -> None:
        data = describe_prompts(self.settings)
        by_group = {group["id"]: [item["name"] for item in group["items"]] for group in data["tools"]}
        self.assertEqual(set(by_group), {"chat", "dream"})
        self.assertEqual(set(by_group["chat"]), CHAT_TOOL_NAMES)
        self.assertEqual(set(by_group["dream"]), DREAM_TOOL_NAMES)
        self.assertEqual(
            len(by_group["chat"]) + len(by_group["dream"]), 16, "工具总数变了"
        )
        for group in data["tools"]:
            for item in group["items"]:
                with self.subTest(tool=item["name"]):
                    self.assertIn(item["state"], VALID_STATES)
                    self.assertTrue(item["description"].strip())
                    self.assertIsInstance(item["parameters"], dict)
                    self.assertEqual(item["id"], item["name"])

    def test_dream_tools_match_what_the_dream_agent_gets(self) -> None:
        """整理侧那 7 个必须与 `build_dream_tools` 真造出来的一致。"""
        data = describe_prompts(self.settings)
        shown = {item["name"]: item for group in data["tools"] for item in group["items"]}
        real = build_dream_tools(
            store=VaultStore(self.settings.vault_dir),
            sessions_dir=self.settings.sessions_dir,
            cursors={},
            tracker=ReadTracker({}),
            record_max_events=self.settings.dream_record_max_events,
            record_max_chars=self.settings.dream_record_max_chars,
            max_result_chars=self.settings.max_tool_result_chars,
            today="",
        )
        for tool in real:
            with self.subTest(tool=tool.name):
                self.assertEqual(shown[tool.name]["description"], tool.description)
                self.assertEqual(shown[tool.name]["parameters"], tool.parameters)

    def _used_prompt_keys(self) -> set[str]:
        """把 `i18n.t` 换成记账版，**跑遍各种配置与各种失败路径**，收集真正被取用的 key。

        为什么必须跑这么多：`ui.prompts.*` 里有三类只在特定情形才被取到 ——
        ① 状态相关的（档案空着 / 有 README / 联网关着 / 搜索关着）；
        ② 只在**写路径**上出现的报错与警告（保存被拒、文件坏了、没配数据目录）；
        ③ 只在某种占位符情形下出现的警告。
        只跑 `describe_prompts` 会把它们全判成死 key —— 这条测试的第一版就误报过两次
        （先是 3 条联网相关的，后来又是我自己新加的 11 条报错文案）。
        **"这条路没走到"不等于"这个东西没用"。**
        """
        from harness import prompt_view as module
        from harness import prompts
        from harness.net.config import TOOLS_FILE, ToolStore

        used: set[str] = set()
        # **两个模块各有一份 `t`**：读路径在 prompt_view、写路径（报错与警告）在
        # prompts。只换一个会漏掉一半 —— 第一版只换了 prompt_view，
        # 于是写路径那 11 条被判成死 key（"没人用"，其实是"没被记账"）。
        originals: list[tuple[object, object]] = [
            (module, module.t),
            (prompts, prompts.t),
        ]
        fallback = module.t

        def spy(key: str, /, **kwargs: object) -> str:
            used.add(key)
            return fallback(key, **kwargs)

        def fresh() -> tuple[tempfile.TemporaryDirectory, Settings]:
            tmp = tempfile.TemporaryDirectory()
            return tmp, test_settings(Path(tmp.name) / "data")

        def with_vault(settings: Settings, *, readme: bool) -> None:
            store = VaultStore(settings.vault_dir)
            store.write_file("sections/a.md", "A")
            if readme:
                store.write_file(README_NAME, "# 索引\n\n- sections/a.md\n")

        def write_paths(settings: Settings) -> None:
            """把写路径上每条报错/警告各触发一次（内容不重要，取到文案就行）。"""
            prompts.configure(settings.data_dir)
            for call in (
                lambda: prompts.save("jp", "SYSTEM_PROMPT", "x"),  # err.language
                lambda: prompts.save("zh", "NOPE", "x"),  # err.no_such_id
                lambda: prompts.save("zh", "SYSTEM_PROMPT", "   "),  # err.empty
                lambda: prompts.save(
                    "zh", "SYSTEM_PROMPT", "字" * 30, max_chars=10
                ),  # err.too_long
                lambda: prompts.save("zh", "SYSTEM_PROMPT", "括号 {"),  # err.bad_template
                lambda: prompts.save("zh", "SYSTEM_PROMPT", "{foo}"),  # err.unknown_placeholder
                lambda: prompts.save(
                    "zh", "SYSTEM_PROMPT", "只留联网：{web_tools_text}"
                ),  # warn.missing_placeholder
            ):
                try:
                    call()
                except Exception:  # noqa: BLE001 - 这些调用本来就是故意失败的
                    pass
            # 坏文件的三条（读不出来 / 结构不对 / 某一段不是对象）
            settings.data_dir.mkdir(parents=True, exist_ok=True)
            for payload in ("{ 这不是 JSON", '{"zh": "不是对象"}', "[]"):
                (settings.data_dir / prompts.OVERRIDES_FILE).write_text(
                    payload, encoding="utf-8"
                )
                prompts.state()
            (settings.data_dir / prompts.OVERRIDES_FILE).unlink(missing_ok=True)
            # 没配数据目录时保存（err.no_data_dir）
            prompts.configure(None)
            try:
                prompts.save("zh", "SYSTEM_PROMPT", "x")
            except Exception:  # noqa: BLE001
                pass

        for target, _spy_unused in originals:
            setattr(target, "t", spy)  # type: ignore[arg-type]
        try:
            for label, setup in (
                ("全新（档案空着）", None),
                ("有档案、没有 README", lambda s: with_vault(s, readme=False)),
                ("有档案、有 README", lambda s: with_vault(s, readme=True)),
                (
                    "联网工具关着",
                    lambda s: ToolStore(s.data_dir / TOOLS_FILE).save_web({"enabled": False}),
                ),
                (
                    "联网开着但搜索关着",
                    lambda s: ToolStore(s.data_dir / TOOLS_FILE).save_web(
                        {"enabled": True, "search_backend": "off"}
                    ),
                ),
            ):
                tmp, settings = fresh()
                try:
                    if setup is not None:
                        setup(settings)
                    describe_prompts(settings)
                finally:
                    prompts.configure(None)
                    tmp.cleanup()
            tmp, settings = fresh()
            try:
                write_paths(settings)
            finally:
                prompts.configure(None)
                tmp.cleanup()
        finally:
            for target, saved in originals:
                setattr(target, "t", saved)  # type: ignore[arg-type]
        return used

    def test_every_catalogued_key_is_actually_used(self) -> None:
        """`ui.prompts.*` 里的每条文案都必须**真的被取用**过。

        这条是补一个真实的缺口：上一轮前端把契约里要求的一个"必填：{list}"
        写进了 catalog，而实现用的是按格子的另一条 —— 于是它成了没人引用的死 key，
        而且**没有任何东西会发现**（守卫只查"两种语言是否对称"，不查"有没有人用"）。
        服务端这边同理，所以这里也加一道。
        """
        catalogued = {key for key in i18n.keys() if key.startswith("ui.prompts.")}
        self.assertTrue(catalogued, "目录里一条 ui.prompts.* 都没有？")
        unused = sorted(catalogued - self._used_prompt_keys())
        self.assertEqual(unused, [], f"这些文案没人用（死 key，删掉或接上）：{unused}")

    def test_summary_counts_add_up(self) -> None:
        data = describe_prompts(self.settings)
        summary = data["summary"]
        self.assertEqual(summary["prompts"], sum(len(g["items"]) for g in data["groups"]))
        self.assertEqual(summary["prompt_groups"], len(data["groups"]))
        self.assertEqual(summary["tools"], sum(len(g["items"]) for g in data["tools"]))
        self.assertEqual(
            summary["tools_on"],
            sum(1 for g in data["tools"] for i in g["items"] if i["state"] == "on"),
        )

    def test_when_and_condition_are_human_sentences(self) -> None:
        data = describe_prompts(self.settings)
        for group in data["groups"]:
            self.assertTrue(group["title"].strip())
            self.assertTrue(group["note"].strip())
            for item in group["items"]:
                with self.subTest(item=item["id"]):
                    self.assertTrue(item["when"].strip(), "每条都要说清什么时候出现")
                    if item["state"] == "off":
                        self.assertTrue(
                            item["condition"].strip(), "off 的条目必须说明为什么"
                        )
        for group in data["tools"]:
            self.assertTrue(group["title"].strip())
            self.assertTrue(group["note"].strip())

    def test_placeholders_are_extracted_in_order(self) -> None:
        data = describe_prompts(self.settings)
        by_id = {item["id"]: item for group in data["groups"] for item in group["items"]}
        # 顺序 = **在原文里出现的顺序**（三个坑的先后就是 system 提示里的先后：
        # 联网 → 档案 → 记忆条目，与 ADR-012/044/056 的"稳定在前"一致）
        self.assertEqual(
            by_id["SYSTEM_PROMPT"]["placeholders"],
            ["web_tools_text", "vault_text", "memory_text"],
        )
        self.assertEqual(by_id["TAIL_NOW"]["placeholders"], ["stamp", "weekday", "offset"])
        # 没有占位符的那些应当是空列表，而不是 None
        self.assertEqual(by_id["DREAM_INSTRUCTION"]["placeholders"], [])


class PromptTruthfulnessTests(unittest.TestCase):
    """页面上写的必须与真正发出去的**同一份**。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = test_settings(Path(self._tmp.name))

    def test_chat_tool_specs_match_a_real_runtime(self) -> None:
        """拿一个真 `Runtime` 装配出来的注册表逐条对比。

        这是这一页最容易说谎的地方：工具的描述与参数 schema 会随语言和代码变，
        抄一份到别处迟早对不上。这里比的是**名字 + 描述 + 参数**三样。
        """
        runtime = Runtime(self.settings, provider=FakeProvider([]))
        try:
            real = {spec["function"]["name"]: spec["function"] for spec in runtime.registry.specs()}
        finally:
            runtime.close()
        data = describe_prompts(self.settings)
        shown = {
            item["name"]: item for group in data["tools"] for item in group["items"]
        }
        # 注册表里有的，页面上必须在（且逐字一致）
        for name, spec in real.items():
            with self.subTest(tool=name):
                self.assertIn(name, shown)
                self.assertEqual(shown[name]["description"], spec["description"])
                self.assertEqual(shown[name]["parameters"], spec["parameters"])
        # 页面上标着"生效"的对话侧工具，注册表里也必须有
        for item in [i for g in data["tools"] if g["id"] == "chat" for i in g["items"]]:
            if item["state"] == "on":
                with self.subTest(tool=item["name"]):
                    self.assertIn(item["name"], real)

    def test_memory_tools_are_built_without_a_real_database(self) -> None:
        """记忆三件套用的是 `:memory:` 替身 —— 参数与原型必须与真库构造的一致。"""
        from harness.memory import MemoryStore

        proxy = MemoryStore(instruction_path=":memory:", max_items=1, max_chars=1000)
        try:
            expected = {
                tool.name: (tool.description, tool.parameters)
                for tool in (MemCreateTool(proxy), MemEditTool(proxy), MemDeleteTool(proxy))
            }
        finally:
            proxy.close()
        data = describe_prompts(self.settings)
        shown = {i["name"]: i for g in data["tools"] for i in g["items"]}
        for name, (description, parameters) in expected.items():
            with self.subTest(tool=name):
                self.assertEqual(shown[name]["description"], description)
                self.assertEqual(shown[name]["parameters"], parameters)


class PromptReadOnlyTests(unittest.TestCase):
    """只读：这一页不许在磁盘上留下任何东西。"""

    def _fresh_settings(self) -> tuple[tempfile.TemporaryDirectory, Settings]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp, test_settings(Path(tmp.name) / "data")

    def test_nothing_is_created_on_disk(self) -> None:
        tmp, settings = self._fresh_settings()
        self.assertFalse(settings.data_dir.exists())
        describe_prompts(settings)
        leftovers = sorted(str(p.relative_to(tmp.name)) for p in Path(tmp.name).rglob("*"))
        self.assertEqual(leftovers, [], "看一眼提示词不该在 data/ 里种出任何文件")

    def test_it_works_when_the_memory_database_does_not_exist(self) -> None:
        """记忆库不存在时也要能用（这正是"零落盘"要的效果）。"""
        _tmp, settings = self._fresh_settings()
        data = describe_prompts(settings)
        self.assertEqual(data["summary"]["tools"], 16)
        # 档案为空 → 那三个工具标成未注册
        vault_tools = [
            item
            for group in data["tools"]
            if group["id"] == "chat"
            for item in group["items"]
            if item["name"].startswith("dream_")
        ]
        self.assertEqual(len(vault_tools), 3)
        self.assertTrue(all(item["state"] == "off" for item in vault_tools))
        self.assertTrue(all(item["condition"] for item in vault_tools))

    def test_broken_config_files_do_not_break_the_page(self) -> None:
        """`tools.json` 坏掉、`vault/sections` 被换成一个文件 —— 这一页都不许 500。

        它是"自查页"，恰恰在环境有点问题时最需要能打开。
        """
        tmp, settings = self._fresh_settings()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        (settings.data_dir / "tools.json").write_text("{ 这不是 JSON", encoding="utf-8")
        data = describe_prompts(settings)
        self.assertEqual(data["summary"]["tools"], 16)

        settings.vault_dir.mkdir(parents=True, exist_ok=True)
        (settings.vault_dir / "sections").write_text("我是文件不是目录", encoding="utf-8")
        data = describe_prompts(settings)
        self.assertEqual(data["summary"]["prompts"], 22)

    def test_vault_tools_turn_on_once_there_is_content(self) -> None:
        """有档案之后那三个工具就该是生效的（状态是**算出来的**，不是写死的）。"""
        tmp, settings = self._fresh_settings()
        VaultStore(settings.vault_dir).write_file("sections/口味.md", "手冲")
        data = describe_prompts(settings)
        vault_tools = [
            item
            for group in data["tools"]
            if group["id"] == "chat"
            for item in group["items"]
            if item["name"].startswith("dream_")
        ]
        self.assertTrue(all(item["state"] == "on" for item in vault_tools))
        # 档案几种提示词的状态也要跟着翻过来
        prompts = {i["id"]: i for g in data["groups"] for i in g["items"]}
        self.assertEqual(prompts["VAULT_EMPTY_PROMPT"]["state"], "off")
        # 有档案但没 README
        self.assertEqual(prompts["VAULT_NO_README_PROMPT"]["state"], "on")
        self.assertEqual(prompts["VAULT_PROMPT"]["state"], "off")


class PromptLanguageTests(unittest.TestCase):
    """整页跟随界面语言（ADR-085）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = test_settings(Path(self._tmp.name))

    def test_english_mode_is_english_and_uses_the_english_prompts(self) -> None:
        with i18n.use("en"):
            data = describe_prompts(self.settings)
            self.assertEqual(data["language"], "en")
            self.assertEqual(
                {item["id"] for group in data["groups"] for item in group["items"]},
                {name for name in dir(prompts_en) if name.isupper()} - PROMPT_SKIP,
                "英文模式下条目集合应当与中文一致（只是内容不同）",
            )
            for group in data["groups"]:
                with self.subTest(group=group["id"]):
                    self.assertFalse(CJK.search(group["title"]), group["title"])
                    self.assertFalse(CJK.search(group["note"]), group["note"])
                for item in group["items"]:
                    with self.subTest(item=item["id"]):
                        # 提示词正文来自英文那一套（不是中文的）
                        self.assertEqual(item["text"], str(getattr(prompts_en, item["id"])))
                        self.assertFalse(CJK.search(item["when"]), item["when"])
                        self.assertFalse(CJK.search(item["condition"]), item["condition"])
            for group in data["tools"]:
                self.assertFalse(CJK.search(group["title"]))
                self.assertFalse(CJK.search(group["note"]))
                for item in group["items"]:
                    with self.subTest(tool=item["name"]):
                        self.assertFalse(CJK.search(item["description"]), item["name"])
                        self.assertFalse(CJK.search(item["condition"]), item["name"])

    def test_switching_language_changes_the_payload(self) -> None:
        zh = describe_prompts(self.settings)
        with i18n.use("en"):
            en = describe_prompts(self.settings)
        self.assertNotEqual(zh["groups"][0]["title"], en["groups"][0]["title"])
        self.assertNotEqual(
            zh["groups"][0]["items"][0]["text"], en["groups"][0]["items"][0]["text"]
        )

    def test_payload_is_json_serialisable(self) -> None:
        """接口直接 `json.dumps` 它 —— 有 set/Path/模块对象藏在里面就会 500。"""
        data = describe_prompts(self.settings)
        text = json.dumps(data, ensure_ascii=False)
        self.assertIn("SYSTEM_PROMPT", text)


if __name__ == "__main__":
    unittest.main()
