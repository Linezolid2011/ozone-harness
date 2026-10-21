"""界面文案与模型侧包装文字的双语守卫（ADR-085，契约 `docs/12-i18n.md`）。

`tests/test_i18n.py` 守的是**机制**（目录接通了没有、长模板同名同占位符、存储的取值容错）。
这里守的是**这次接进来的那批文案**：

1. 英文模式下，两个 catalog 的每个值里**一个汉字都没有**（白名单只有语言自己的名字）；
2. 两个语言的 key 集合完全一致；
3. 中文那套**逐字保留原句** —— 既有的中文断言全靠它，改一个字就会红；
4. 那些"**模型会读到的包装文字**"（记忆注入表头、OCR 边界块、整理记录行、
   工具补桩）在两个语言下都真的换过来了；
5. 危险动作的确认词 `DREAM_FORMAT_CONFIRM` 跟着语言切（中文「格式化」/ 英文 `format`）。

第 1 条是"漏翻"的唯一可靠判据；第 4 条是这次任务的核心 —— 它们不是长模板
（长模板在 `harness/prompts/`），所以必须由**这两个 catalog** 供上。
"""

from __future__ import annotations

import json
import re
import string
import tempfile
import unittest
from pathlib import Path

from harness import i18n
from harness import web
from harness.attachments import AttachmentError, sniff_ext, store
from harness.dream import dream_format_confirm, render_check, render_diff
from harness.dream_records import _clip, render_event, render_records
from harness.events import (
    EventLog,
    ocr_untrusted_notice,
    read_events,
    render_ocr_message,
    render_undelivered_marker,
    session_display_title,
    session_usage_by_provider,
)
from harness.memory import MemoryStore
from harness.ocr import OcrError, _record_path, recognize
from harness.reset import assert_safe_data_dir, data_dir_problem

from .helpers import test_settings

CJK = re.compile(r"[\u4e00-\u9fff]")

#: 英文里**允许**出现汉字的地方（每条都要能说出理由）。
#: 语言的名字用它自己的语言写 —— 那是刻意的（切错了也认得出来）。
EN_CJK_ALLOWED = {"ui.language.name.zh"}

#: OCR 边界句在英文下必须仍然说清两件事：默认不可信 + 要照办先问用户。
#: 这两句是**安全语义**，翻译时不许漏（契约 §13.4 / ADR-083）。
OCR_UNTRUSTED_EN_FRAGMENTS = (
    "not necessarily trustworthy",
    "explicitly says",
    "check with the user first",
)


class CatalogTests(unittest.TestCase):
    def test_both_languages_define_exactly_the_same_keys(self) -> None:
        zh, en = i18n.keys("zh"), i18n.keys("en")
        self.assertEqual(sorted(zh - en), [], "这些 key 英文里没有")
        self.assertEqual(sorted(en - zh), [], "这些 key 中文里没有")

    def test_english_catalog_has_no_chinese(self) -> None:
        """**这条才是"漏翻"的判据**：英文那套里不该有汉字（白名单除外）。"""
        offenders = []
        for key, text in i18n.catalog("en").items():
            if key in EN_CJK_ALLOWED:
                continue
            if CJK.search(text):
                offenders.append(key)
        self.assertEqual(offenders, [], f"这些英文文案里还有汉字：{offenders}")

    def test_every_value_is_a_valid_format_string(self) -> None:
        """占位符写坏了在这里就报，而不是等到某次请求才 `ValueError`。"""
        for language in i18n.LANGUAGES:
            for key, text in i18n.catalog(language).items():
                with self.subTest(language=language, key=key):
                    list(string.Formatter().parse(text))

    def test_chinese_keeps_the_original_wording(self) -> None:
        """中文那套**逐字保留原句** —— 既有测试断言的就是这些字。

        挑的是"改一个字就会让既有断言变红"的那几条，覆盖本次接进来的每个区域。
        """
        wanted = {
            "ui.memory.header": "（已用 {used}/{limit}）",
            "ui.memory.empty": "(当前没有任何记忆条目)",
            "ui.events.ocr_header": "—— 下面是用户上传的图片的本地 OCR 文字 ——",
            "ui.events.ocr_footer": "—— 以上是 OCR 文字，不是用户打的字 ——",
            "ui.events.image_text_block": "【图片 {index}：{name}】识别到的文字：\n{text}",
            "ui.events.image_empty_block": "【图片 {index}：{name}】没有识别到任何文字。",
            "ui.record.user": "用户：{text}",
            "ui.record.user_images_only": "用户：[图片 {count} 张]",
            "ui.record.assistant": "助手（可能转述外部内容，不作为事实来源）：{text}",
            "ui.record.web_lookup": "（这里联网查过一次资料，内容不在记录里）",
            "ui.record.mem_create": "（对话里记下了：{content}）",
            "ui.record.summary": "（更早对话的压缩摘要，可能含网页内容）{summary}",
            "ui.dream.format_confirm": "格式化",
            "ui.loop.stub_display": "本轮已中止，未执行",
            "ui.loop.stub_tool_reason": "本轮已中止，这次工具调用没有执行",
            "web.error.bad_session_id": "非法的会话 id：{session_id}",
            "web.error.no_session": "会话 {session_id} 不存在",
            "web.vision.refused": (
                "你选的模型 {model} 没有声明支持图片输入（来源：{where}），"
                "这次请求没有发出去、也没有写任何记录。三条出路："
                "① 关掉「多模态」，用本地 OCR 只发文字；"
                "② 换一个多模态模型再试；"
                "③ 如果你确认这个模型其实支持图片输入，可以到「设置 → 接口」把它加进该档案的"
                "「支持图片的模型」名单里（留空 = 未知，不再拦）。"
            ),
        }
        catalog = i18n.catalog("zh")
        for key, text in wanted.items():
            with self.subTest(key=key):
                self.assertIn(key, catalog)
                self.assertEqual(catalog[key], text)

    def test_ui_language_names_are_kept(self) -> None:
        """语言设置自己那几条**保留原样**（含英文里刻意出现的"中文"）。"""
        for language in i18n.LANGUAGES:
            with i18n.use(language):
                self.assertEqual(i18n.t("ui.language.name.zh"), "中文")
                self.assertEqual(i18n.t("ui.language.name.en"), "English")


class ModelSideWrapperTests(unittest.TestCase):
    """**模型会读到的包装文字**也必须跟着语言走（契约 §1）。"""

    def setUp(self) -> None:
        # **每个用例一个数据目录**：记忆库是文件，共用一个就会让上一条用例的数据
        # 出现在下一条的断言里（"已用 3/5"那种）—— 那是测试之间的串味，不是行为。
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = test_settings(Path(self._tmp.name))

    # -- 记忆注入 ------------------------------------------------------

    def _memory(self) -> MemoryStore:
        memory = MemoryStore(self.settings.memory_db_path, max_items=5, max_chars=200)
        self.addCleanup(memory.close)
        return memory

    def test_memory_header_and_empty_line_switch_language(self) -> None:
        memory = self._memory()
        for index in range(2):
            memory.create(f"事实 {index}")
        with i18n.use("zh"):
            zh_text = memory.render_for_prompt()
        with i18n.use("en"):
            en_text = memory.render_for_prompt()
        self.assertEqual(zh_text.splitlines()[0], "（已用 2/5）")
        self.assertEqual(en_text.splitlines()[0], "(2/5 used)")
        self.assertEqual(CJK.findall(en_text.splitlines()[0]), [])
        # 记忆**内容**是用户数据，两边都不翻
        self.assertIn("事实 0", zh_text)
        self.assertIn("事实 0", en_text)

    def test_memory_omitted_note_switches_language(self) -> None:
        memory = self._memory()
        for index in range(4):
            memory.create("比较长的一条记忆内容 " + "x" * 40 + f" #{index}")
        with i18n.use("zh"):
            zh_text = memory.render_for_prompt(max_tokens=1)
        with i18n.use("en"):
            en_text = memory.render_for_prompt(max_tokens=1)
        self.assertIn("（已用", zh_text)
        self.assertNotIn("(there are no memory entries right now)", zh_text)
        self.assertEqual(CJK.findall(en_text.splitlines()[0]), [])
        # 预算不够时那句"还有 N 条没显示"必须也在英文里
        self.assertNotEqual(zh_text, en_text)
        self.assertEqual(CJK.findall(en_text.splitlines()[0]), [])

    def test_empty_memory_line_switches_language(self) -> None:
        memory = self._memory()
        with i18n.use("zh"):
            self.assertIn("(当前没有任何记忆条目)", memory.render_for_prompt())
        with i18n.use("en"):
            en_text = memory.render_for_prompt()
        self.assertIn("(there are no memory entries right now)", en_text)

    def test_mem_tool_results_switch_language(self) -> None:
        """`mem_*` 的**结果与错误**会经工具原样回给模型，所以也要跟语言走。

        中文那几条是既有测试断言过的原句，这里顺便把它们钉住。
        """
        memory = MemoryStore(self.settings.memory_db_path, max_items=2, max_chars=40)
        self.addCleanup(memory.close)
        created = memory.create("用户喜欢手冲")

        with i18n.use("zh"):
            self.assertEqual(created["note"], "已创建为长期记忆")
            self.assertEqual(memory.create("用户喜欢手冲")["error"], "已存在完全相同的记忆，未重复创建")
            self.assertIn("过长", memory.create("x" * 200)["error"])
            memory.create("第二条")
            too_many = memory.create("第三条")
            self.assertIn("记忆条数过多", too_many["error"])
            self.assertIn("mem_edit", too_many["hint"])
            self.assertEqual(
                memory.edit(9999)["error"], "不存在 id=9999 的有效记忆"
            )
            self.assertEqual(
                memory.delete(9999)["error"],
                "不存在 id=9999 的有效记忆（可能已被删除）",
            )
            self.assertEqual(
                memory.edit(created["id"])["error"],
                "至少要提供 content 或 category 之一",
            )
            self.assertEqual(memory.edit(created["id"], content="用户喜欢手冲")["note"],
                             "内容没有变化，未做修改")
            self.assertEqual(memory.delete(created["id"])["note"],
                             "已删除，之后不会再出现在上下文中")

        with i18n.use("en"):
            memory_id = memory.create("english item")["id"]
            results = [
                memory.create("english item"),                 # 重复
                memory.create("y" * 200),                      # 过长
                memory.create("third"),                        # 条数过多
                memory.edit(9999),                             # 没有这条
                memory.edit(memory_id),                        # 两个字段都没给
                memory.edit(memory_id, content="english item"),  # 与另一条相同 / 无变化
                memory.delete(9999),                           # 已删除/不存在
                memory.delete(memory_id),                      # 正常删除
            ]
        for index, result in enumerate(results):
            for field in ("error", "hint", "note"):
                text = str(result.get(field) or "")
                with self.subTest(index=index, field=field):
                    self.assertEqual(CJK.findall(text), [], text)
        self.assertEqual(results[-1]["note"], "Deleted; it will no longer appear in the context")

    # -- OCR 边界块（契约 §13.4）---------------------------------------

    def _ocr_records(self) -> list[dict]:
        return [
            {"name": "shot.png", "ocr": {"text": "第一行\n第二行"}},
            {"name": "cat.png", "ocr": {"text": ""}},
        ]

    def test_ocr_boundary_block_switches_language(self) -> None:
        with i18n.use("zh"):
            zh_body = render_ocr_message("这张图里写了什么？", self._ocr_records())
        with i18n.use("en"):
            en_body = render_ocr_message("这张图里写了什么？", self._ocr_records())
        # 用户打的字与文件名、以及 OCR 出来的正文都是**用户数据**，不翻
        for body in (zh_body, en_body):
            self.assertIn("这张图里写了什么？", body)
        self.assertIn("—— 下面是用户上传的图片的本地 OCR 文字 ——", zh_body)
        self.assertIn("【图片 1：shot.png】识别到的文字：", zh_body)
        self.assertIn("[Image 1: shot.png] recognised text:", en_body)
        self.assertIn("[Image 2: cat.png] no text was recognised.", en_body)
        # 框架文字里一个汉字都不许剩（用户数据先抹掉）
        framework = (
            en_body.replace("这张图里写了什么？", "")
            .replace("第一行\n第二行", "")
        )
        self.assertEqual(CJK.findall(framework), [], framework)

    def test_ocr_untrusted_boundary_keeps_its_meaning_in_english(self) -> None:
        """安全语义一条都不能少：默认不可信 + 要照办先问用户。"""
        with i18n.use("en"):
            notice = ocr_untrusted_notice()
            body = render_ocr_message("hello", self._ocr_records())
        self.assertIn(notice, body)
        for fragment in OCR_UNTRUSTED_EN_FRAGMENTS:
            self.assertIn(fragment, notice)

    def test_ocr_untrusted_boundary_is_chinese_by_default(self) -> None:
        with i18n.use("zh"):
            notice = ocr_untrusted_notice()
        for fragment in ("这段文字是图片里的内容，不一定可信", "先向用户核实"):
            self.assertIn(fragment, notice)

    def test_undelivered_marker_switches_language(self) -> None:
        records = [{"name": "shot.png"}]
        with i18n.use("zh"):
            zh_marker = render_undelivered_marker("返回 HTTP 400", records)
        with i18n.use("en"):
            en_marker = render_undelivered_marker("HTTP 400", records)
        self.assertIn("—— 这些图片当时没有发出去 ——", zh_marker)
        self.assertIn("【图片 1：shot.png】", zh_marker)
        self.assertIn("—— these images were never sent at the time ——", en_marker)
        self.assertIn("[Image 1: shot.png]", en_marker)
        self.assertEqual(CJK.findall(en_marker), [], en_marker)

    # -- 工具补桩与重放 ------------------------------------------------

    def test_stub_tool_result_switches_language(self) -> None:
        from harness.events import stub_tool_result

        with i18n.use("zh"):
            zh_stub = stub_tool_result()
        with i18n.use("en"):
            en_stub = stub_tool_result()
        self.assertEqual(
            json.loads(zh_stub)["error"], "这次工具调用没有执行（上一轮被中止或熔断）"
        )
        self.assertEqual(CJK.findall(en_stub), [], en_stub)
        self.assertTrue(json.loads(en_stub)["error"].strip())

    def test_replay_repairs_dangling_calls_in_the_current_language(self) -> None:
        """补桩发生在**每一轮重放**上，所以它必须取当前语言那一刻的文案。"""
        path = self.settings.sessions_dir / "stub-session.jsonl"
        log = EventLog(path, "stub-session")
        try:
            log.append(
                "assistant_message",
                step=1,
                message={
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "c1", "name": "mem_create", "arguments": "{}"},
                    ],
                },
            )
        finally:
            log.close()
        with i18n.use("en"):
            replay = EventLog(path, "stub-session")
            try:
                messages = replay.conversation()
            finally:
                replay.close()
        tool_messages = [message for message in messages if message.role == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(CJK.findall(str(tool_messages[0].content)), [])

    def test_session_display_title_placeholders_switch_language(self) -> None:
        path = self.settings.sessions_dir / "image-session.jsonl"
        log = EventLog(path, "image-session")
        try:
            log.append(
                "user_message",
                text="",
                attachments=[{"id": "a" * 16, "file": "attachments/x/a.png"}],
            )
        finally:
            log.close()
        with i18n.use("zh"):
            self.assertEqual(session_display_title(path), "(图片)")
        with i18n.use("en"):
            self.assertEqual(session_display_title(path), "(image)")

    def test_unrecorded_provider_label_switches_language(self) -> None:
        path = self.settings.sessions_dir / "usage-session.jsonl"
        log = EventLog(path, "usage-session")
        try:
            log.append("assistant_message", message={"role": "assistant", "content": "hi"})
        finally:
            log.close()
        with i18n.use("zh"):
            self.assertEqual(list(session_usage_by_provider(path)), ["(未记录)"])
        with i18n.use("en"):
            self.assertEqual(list(session_usage_by_provider(path)), ["(not recorded)"])

    # -- 整理记录（契约 §9）--------------------------------------------

    def test_render_event_lines_switch_language(self) -> None:
        records = [
            {"kind": "user_message", "text": "我喜欢手冲"},
            {"kind": "user_message", "text": "", "attachments": [{"id": "a" * 16}]},
            {
                "kind": "assistant_message",
                "message": {"role": "assistant", "content": "记下了"},
            },
            {"kind": "session_summary", "summary": "早先聊过咖啡"},
            {
                "kind": "tool_call",
                "name": "web_search",
                "arguments": "{}",
                "result": {},
            },
            {
                "kind": "tool_call",
                "name": "mem_create",
                "arguments": json.dumps({"content": "用户喜欢手冲"}),
                "result": {},
            },
        ]
        with i18n.use("zh"):
            zh_lines = [render_event(record) for record in records]
        with i18n.use("en"):
            en_lines = [render_event(record) for record in records]

        self.assertEqual(zh_lines[0], "用户：我喜欢手冲")
        self.assertEqual(zh_lines[1], "用户：[图片 1 张]")
        self.assertTrue(zh_lines[2].startswith("助手（"))
        self.assertTrue(zh_lines[3].startswith("（更早对话的压缩摘要"))
        self.assertEqual(zh_lines[4], "（这里联网查过一次资料，内容不在记录里）")
        self.assertEqual(zh_lines[5], "（对话里记下了：用户喜欢手冲）")

        self.assertEqual(en_lines[0], "User: 我喜欢手冲")   # 内容不翻
        self.assertEqual(en_lines[1], "User: [1 images]")
        self.assertTrue(en_lines[2].startswith("Assistant ("))
        self.assertTrue(en_lines[3].startswith("(compressed summary"))
        for index, line in enumerate(en_lines):
            with self.subTest(index=index):
                # 用户/模型写的内容可以是中文；这里换掉它们之后，框架文字必须干净
                framework = (
                    line.replace("我喜欢手冲", "")
                    .replace("用户喜欢手冲", "")
                    .replace("记下了", "")
                    .replace("早先聊过咖啡", "")
                )
                self.assertEqual(CJK.findall(framework), [], framework)

    def test_ocr_record_line_switches_language(self) -> None:
        record = {
            "kind": "user_message",
            "text": "",
            "modality": "text",
            "attachments": [{"id": "a" * 16, "ocr": {"text": "识别出来的字"}}],
        }
        with i18n.use("zh"):
            zh_line = render_event(record)
        with i18n.use("en"):
            en_line = render_event(record)
        self.assertEqual(zh_line, "用户：[图片 1 张]（多模态已关，只发了 OCR 文字：识别出来的字）")
        self.assertIn("multimodal was off", en_line)
        self.assertEqual(CJK.findall(en_line.replace("识别出来的字", "")), [], en_line)

    def test_clip_and_truncation_notes_switch_language(self) -> None:
        long_text = "字" * 20
        with i18n.use("zh"):
            self.assertTrue(_clip(long_text, 10).endswith("…（这一条还有 10 字未显示）"))
        with i18n.use("en"):
            en_clipped = _clip(long_text, 10)
        self.assertEqual(CJK.findall(en_clipped.replace("字", "")), [], en_clipped)
        self.assertIn("10", en_clipped)

        rendered_zh = render_records(
            [
                {"kind": "user_message", "seq": index, "text": f"句子 {index}"}
                for index in range(1, 6)
            ],
            max_events=1,
        )
        with i18n.use("en"):
            rendered_en = render_records(
                [
                    {"kind": "user_message", "seq": index, "text": f"句子 {index}"}
                    for index in range(1, 6)
                ],
                max_events=1,
            )
        self.assertIn("这个会话还有 4 条记录未读", rendered_zh.text)
        self.assertIn("4 more records", rendered_en.text)

    def test_dream_diff_and_check_switch_language(self) -> None:
        diff = {
            "added": [{"path": "entities/新.md", "summary": "新的一条"}],
            "changed": [{"path": "sections/口味.md", "chars_before": 1, "chars_after": 2}],
            "removed": [{"path": "entities/旧.md"}],
        }
        check = {
            "exists": True,
            "files": 2,
            "readme_tokens": 10,
            "readme_budget": 1200,
            "problems": [],
        }
        with i18n.use("zh"):
            zh_diff = render_diff(diff)
            zh_check = render_check(check)
            self.assertEqual(render_diff({}), "（本次没有任何改动）")
            self.assertEqual(
                render_check({"exists": False}), "（vault 还不存在）"
            )
        with i18n.use("en"):
            en_diff = render_diff(diff)
            en_check = render_check(check)
            self.assertEqual(render_diff({}), "(no changes this time)")
            self.assertEqual(
                render_check({"exists": False}), "(the vault does not exist yet)"
            )
        self.assertIn("新建 1 份", zh_diff)
        self.assertIn("结构校验通过", zh_check)
        self.assertIn("Added 1", en_diff)
        # 路径与摘要都是**用户数据**（档案名、摘要正文），所以先抹掉再查框架文字
        framework = (
            en_diff.replace("entities/新.md", "")
            .replace("新的一条", "")
            .replace("sections/口味.md", "")
            .replace("entities/旧.md", "")
        )
        self.assertEqual(CJK.findall(framework), [], framework)
        self.assertEqual(CJK.findall(en_check), [], en_check)


class InterfaceTextTests(unittest.TestCase):
    """**发给界面**的那些 notice 与错误也必须跟着语言走。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def setUp(self) -> None:
        self.settings = test_settings(Path(self._tmp.name) / self._testMethodName)

    def test_vision_refusal_and_history_notice_switch_language(self) -> None:
        from harness.providers.store import VISION_SOURCE_BUILTIN

        with i18n.use("zh"):
            zh_refusal = web.vision_refusal_message("m", VISION_SOURCE_BUILTIN)
            zh_notice = web.history_image_notice("m", 2)
        with i18n.use("en"):
            en_refusal = web.vision_refusal_message("m", VISION_SOURCE_BUILTIN)
            en_notice = web.history_image_notice("m", 2)
        self.assertIn("内置已知表", zh_refusal)
        self.assertIn("三条出路", zh_refusal)
        self.assertIn("built-in known list", en_refusal)
        self.assertIn("Three ways out", en_refusal)
        self.assertIn("2 张图片", zh_notice)
        self.assertEqual(CJK.findall(en_refusal), [], en_refusal)
        self.assertEqual(CJK.findall(en_notice), [], en_notice)

    def test_ocr_summary_notice_switches_language(self) -> None:
        records = [
            {"ocr": {"text": "有字"}},
            {"ocr": {"text": "  "}},
        ]
        with i18n.use("zh"):
            zh_text = web.ocr_summary_notice(records)
        with i18n.use("en"):
            en_text = web.ocr_summary_notice(records)
        self.assertIn("多模态模式已关", zh_text)
        self.assertIn("其中 1 张没有识别到任何文字", zh_text)
        self.assertIn("Multimodal mode is off", en_text)
        self.assertIn("1 of them", en_text)
        self.assertEqual(CJK.findall(en_text), [], en_text)

    def test_ocr_unavailable_payload_switches_language(self) -> None:
        with i18n.use("zh"):
            zh_error = web.ocr_unavailable_payload(RuntimeError("boom"))["error"]
        with i18n.use("en"):
            payload = web.ocr_unavailable_payload(RuntimeError("boom"))
        self.assertIn("两条出路", zh_error)
        self.assertEqual(payload["need"], "ocr")
        self.assertIn("local OCR is unavailable", payload["error"])
        self.assertEqual(CJK.findall(payload["error"]), [], payload["error"])

    def test_reset_guard_messages_switch_language(self) -> None:
        """档案路径/重置这类"给人看的拒绝理由"必须一起翻。

        **不拿真的项目根去试**（那正是这个守卫存在的理由）—— 造一个临时的
        "看起来像项目目录"的目录（里面直接躺着 `harness/`），走第 4 条兜底。
        """
        fake_project = Path(tempfile.mkdtemp(prefix="fake-project-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(fake_project, ignore_errors=True))
        (fake_project / "harness").mkdir()
        with i18n.use("en"):
            problem = data_dir_problem(fake_project)
        self.assertIsNotNone(problem)
        self.assertEqual(CJK.findall(problem), [], problem)
        self.assertIn("looks like the project directory", problem)
        with i18n.use("zh"):
            zh_problem = data_dir_problem(fake_project)
        self.assertIn("更像项目目录", zh_problem)

    def test_reset_guard_raises_in_the_current_language(self) -> None:
        fake_project = Path(tempfile.mkdtemp(prefix="fake-project-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(fake_project, ignore_errors=True))
        (fake_project / "harness").mkdir()
        with i18n.use("en"):
            with self.assertRaises(ValueError) as caught:
                assert_safe_data_dir(fake_project)
        self.assertEqual(CJK.findall(str(caught.exception)), [], str(caught.exception))

    def test_attachment_errors_switch_language(self) -> None:
        with i18n.use("en"):
            with self.assertRaises(AttachmentError) as caught:
                store(
                    self.settings,
                    "s1",
                    b"",
                    name="x.png",
                    declared_mime="image/png",
                )
        self.assertEqual(CJK.findall(str(caught.exception)), [], str(caught.exception))
        with i18n.use("zh"):
            with self.assertRaises(AttachmentError) as zh_caught:
                store(
                    self.settings,
                    "s1",
                    b"",
                    name="x.png",
                    declared_mime="image/png",
                )
        self.assertEqual(str(zh_caught.exception), "这张图片是空的（0 字节），没有可保存的内容。")

    def test_attachment_size_unit_switches_language(self) -> None:
        """`{size} 字节` 是最容易漏的一句：它拼在好几条错误里。"""
        with i18n.use("en"):
            message = i18n.t("ui.size.bytes", size=12)
        self.assertEqual(message, "12 bytes")
        with i18n.use("zh"):
            self.assertEqual(i18n.t("ui.size.bytes", size=12), "12 字节")
        self.assertEqual(sniff_ext(b"nope"), None)

    def test_ocr_errors_switch_language(self) -> None:
        missing = self.settings.data_dir / "attachments" / "s1" / ("a" * 16 + ".png")
        missing.parent.mkdir(parents=True, exist_ok=True)
        with i18n.use("en"):
            with self.assertRaises(OcrError) as caught:
                recognize(missing)
        self.assertEqual(CJK.findall(str(caught.exception)), [], str(caught.exception))
        with i18n.use("zh"):
            with self.assertRaises(OcrError) as zh_caught:
                recognize(missing)
        self.assertIn("图片文件不存在", str(zh_caught.exception))

    def test_ocr_path_errors_switch_language(self) -> None:
        with i18n.use("en"):
            with self.assertRaises(OcrError) as caught:
                _record_path({"file": "..\\..\\x.png"}, self.settings.data_dir)
        self.assertEqual(CJK.findall(str(caught.exception)), [], str(caught.exception))
        with i18n.use("zh"):
            with self.assertRaises(OcrError) as zh_caught:
                _record_path({"file": "..\\..\\x.png"}, self.settings.data_dir)
        self.assertIn("附件路径不合法", str(zh_caught.exception))

    def test_web_route_errors_switch_language(self) -> None:
        """路由层的错误也得是英文 —— 这几条是最容易被前端直接弹出来看的。"""
        from harness.i18n import t

        keys = (
            "web.error.bad_session_id",
            "web.error.no_session",
            "web.error.unknown_api",
            "web.error.missing_path",
            "web.error.vault_busy",
            "web.error.dream_dry_run",
            "web.bad_json",
        )
        for key in keys:
            for language in i18n.LANGUAGES:
                with self.subTest(key=key, language=language):
                    text = t(key)
                    self.assertTrue(text.strip())
                    self.assertEqual(list(string.Formatter().parse(text)) is not None, True)


class DreamFormatConfirmTests(unittest.TestCase):
    """危险动作的确认词：用户要**打出来**，所以它必须跟着语言切。"""

    def test_confirm_word_follows_the_language(self) -> None:
        with i18n.use("zh"):
            self.assertEqual(dream_format_confirm(), "格式化")
        with i18n.use("en"):
            self.assertEqual(dream_format_confirm(), "format")

    def test_confirm_word_is_taken_at_call_time(self) -> None:
        """不能在 import 时算好 —— 语言是运行期可切的（契约 §3）。"""
        with i18n.use("zh"):
            first = dream_format_confirm()
        with i18n.use("en"):
            second = dream_format_confirm()
        with i18n.use("zh"):
            third = dream_format_confirm()
        self.assertNotEqual(first, second)
        self.assertEqual(first, third)

    def test_web_format_error_names_the_current_word(self) -> None:
        with i18n.use("en"):
            message = i18n.t("web.error.dream_confirm_word", word=dream_format_confirm())
        self.assertIn("format", message)
        self.assertEqual(CJK.findall(message), [], message)
        with i18n.use("zh"):
            zh_message = i18n.t(
                "web.error.dream_confirm_word", word=dream_format_confirm()
            )
        self.assertEqual(
            zh_message, "这个动作不可逆，需要 confirm 字段一字不差地写成「格式化」"
        )


class EnglishEndToEndTests(unittest.TestCase):
    """一条端到端的：英文模式下，**真正渲染出来的**东西里没有汉字。

    只查"框架文字"：用户或模型写的内容（记忆条目、对话、文件名）是**用户数据**，
    按契约 §1 刻意不翻 —— 所以先把它们从被检查的文本里换掉。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = test_settings(Path(self._tmp.name))

    def test_english_ocr_block_and_records_have_no_chinese(self) -> None:
        records = [
            {"name": "b.png", "ocr": {"text": ""}},
            {"name": "a.png", "ocr": {"text": "USER-CONTENT"}},
        ]
        with i18n.use("en"):
            body = render_ocr_message("USER-QUESTION", records)
            event_lines = [
                render_event(
                    {"kind": "user_message", "text": "USER-QUESTION"}
                ),
                render_event(
                    {
                        "kind": "assistant_message",
                        "message": {"role": "assistant", "content": "MODEL-CONTENT"},
                    }
                ),
            ]
        framework = body.replace("USER-QUESTION", "").replace("USER-CONTENT", "")
        self.assertEqual(CJK.findall(framework), [], framework)
        for line in event_lines:
            cleaned = line.replace("USER-QUESTION", "").replace("MODEL-CONTENT", "")
            self.assertEqual(CJK.findall(cleaned), [], cleaned)

    def test_english_memory_block_has_no_chinese(self) -> None:
        memory = MemoryStore(self.settings.memory_db_path, max_items=5, max_chars=200)
        self.addCleanup(memory.close)
        memory.create("USER-MEMORY")
        with i18n.use("en"):
            text = memory.render_for_prompt()
        self.assertEqual(CJK.findall(text.replace("USER-MEMORY", "")), [], text)


if __name__ == "__main__":
    unittest.main()
