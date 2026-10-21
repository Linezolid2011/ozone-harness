"""多模态（图片）消息的形态与安全测试。

覆盖三件事：
  1. **块形状**：OpenAI 的 `image_url` 与 Anthropic 的 `image/source`，同一组用例
     跑两边 —— 形状不同、取舍一致（文本块在最前、只发图时不发空文本块、多图有序）；
  2. **脱敏硬规则**：走过一整轮 `AgentLoop.run(..., attachments=...)`，会话 JSONL 的
     **每一行**都不许出现 base64（prompt_built 每步都写一次，漏一次日志就毁了）；
  3. **历史重放**：第二轮的历史里仍然带着图片（用户选了"历史图片全部重发"）。

这些用例**不需要 Pillow**：图片块关心的是形状，文件内容是什么字节并不重要；
尺寸来自事件记录。所以"无依赖模式"下它们照样跑。
"""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from harness import attachments
from harness.config import Settings
from harness.events import EventLog, session_display_title
from harness.llm_types import (
    ImagePart,
    Message,
    estimate_image_tokens,
    estimate_message_tokens,
    estimate_messages_tokens,
)
from harness.providers.anthropic_compat import AnthropicCompatProvider
from harness.providers.openai_compat import OpenAICompatProvider

from .helpers import make_harness, test_settings, text_reply

# 一张真的 1×1 PNG。用它而不是随便一段字节，是为了让"data URL 里编码的确实是
# 那张图的字节"这件事可以被逐字节核对。
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
NOTE_A = "a" * 16
NOTE_B = "b" * 16
SESSION = "s1"


class VisionCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.settings = test_settings(self.tmp, api_key="test-key")

    def make_part(
        self,
        note_id: str = NOTE_A,
        *,
        session_id: str = SESSION,
        name: str = "image.png",
        width: int = 1300,
        height: int = 783,
        payload: bytes = TINY_PNG,
    ) -> ImagePart:
        path = self.tmp / "attachments" / session_id / f"{note_id}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return ImagePart(
            path=path, mime="image/png", name=name, width=width, height=height, id=note_id
        )

    def record(
        self,
        note_id: str = NOTE_A,
        *,
        session_id: str = SESSION,
        name: str = "image.png",
        width: int = 1300,
        height: int = 783,
    ) -> dict:
        return {
            "id": note_id,
            "file": f"attachments/{session_id}/{note_id}.png",
            "name": name,
            "mime": "image/png",
            "bytes": len(TINY_PNG),
            "width": width,
            "height": height,
            "stored_bytes": len(TINY_PNG),
            "stored_width": width,
            "stored_height": height,
        }


# --------------------------------------------------------------------------
# OpenAI 兼容：content 变成块数组
# --------------------------------------------------------------------------


class OpenAIBlockTests(VisionCase):
    def test_text_block_comes_first_then_images_in_order(self) -> None:
        first = self.make_part(NOTE_A, name="a.png")
        second = self.make_part(NOTE_B, name="b.png", payload=TINY_PNG + b"\x00")
        payload = Message(role="user", content="这是什么动物", images=[first, second]).to_api()

        self.assertEqual(payload["role"], "user")
        blocks = payload["content"]
        self.assertEqual([block["type"] for block in blocks], ["text", "image_url", "image_url"])
        self.assertEqual(blocks[0]["text"], "这是什么动物")
        self.assertTrue(blocks[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertNotEqual(blocks[1]["image_url"]["url"], blocks[2]["image_url"]["url"])

    def test_image_only_message_has_no_empty_text_block(self) -> None:
        payload = Message(role="user", content="", images=[self.make_part()]).to_api()
        self.assertEqual([block["type"] for block in payload["content"]], ["image_url"])

    def test_data_url_encodes_the_stored_bytes(self) -> None:
        part = self.make_part(payload=TINY_PNG)
        url = Message(role="user", content="x", images=[part]).to_api()["content"][1]
        encoded = url["image_url"]["url"].split("base64,", 1)[1]
        self.assertEqual(base64.b64decode(encoded), TINY_PNG)

    def test_text_only_and_other_roles_are_untouched(self) -> None:
        """没有图时形状一个字都不能变（老日志、老测试全都依赖它）。"""
        self.assertEqual(Message(role="user", content="你好").to_api(), {"role": "user", "content": "你好"})
        # 官方规定 system/assistant 带图直接 400，所以即使有人挂上了也不该变成块数组
        stray = Message(role="assistant", content="好的", images=[self.make_part()])
        self.assertEqual(stray.to_api()["content"], "好的")

    def test_provider_payload_keeps_the_blocks(self) -> None:
        """provider 层不许把 content 重新拼成字符串（那会静默丢图）。"""
        provider = OpenAICompatProvider(self.settings)
        message = Message(role="user", content="看图", images=[self.make_part()])
        payload = provider.build_payload([message])
        blocks = payload["messages"][0]["content"]
        self.assertIsInstance(blocks, list)
        self.assertEqual([block["type"] for block in blocks], ["text", "image_url"])


# --------------------------------------------------------------------------
# Anthropic 兼容：image 内容块
# --------------------------------------------------------------------------


class AnthropicBlockTests(VisionCase):
    def setUp(self) -> None:
        super().setUp()
        self.provider = AnthropicCompatProvider(self.settings)

    def test_image_block_shape_and_order(self) -> None:
        part = self.make_part()
        payload = self.provider.build_payload(
            [Message(role="user", content="这是什么动物", images=[part])]
        )
        blocks = payload["messages"][0]["content"]
        self.assertEqual([block["type"] for block in blocks], ["text", "image"])
        self.assertEqual(blocks[0], {"type": "text", "text": "这是什么动物"})
        source = blocks[1]["source"]
        self.assertEqual(source["type"], "base64")
        self.assertEqual(source["media_type"], "image/png")
        self.assertEqual(base64.b64decode(source["data"]), TINY_PNG)

    def test_image_only_message_has_no_empty_text_block(self) -> None:
        payload = self.provider.build_payload(
            [Message(role="user", content="", images=[self.make_part()])]
        )
        self.assertEqual(
            [block["type"] for block in payload["messages"][0]["content"]], ["image"]
        )

    def test_multiple_images_keep_the_user_order(self) -> None:
        first = self.make_part(NOTE_A, name="a.png", payload=TINY_PNG)
        second = self.make_part(NOTE_B, name="b.png", payload=TINY_PNG + b"\x00")
        payload = self.provider.build_payload(
            [Message(role="user", content="", images=[first, second])]
        )
        blocks = payload["messages"][0]["content"]
        self.assertEqual(len(blocks), 2)
        self.assertNotEqual(
            base64.b64decode(blocks[0]["source"]["data"]),
            base64.b64decode(blocks[1]["source"]["data"]),
        )

    def test_text_only_user_messages_still_merge_as_strings(self) -> None:
        """带图分支不能顺手改掉老行为：连续两条纯文本 user 仍要合并成一条字符串。"""
        payload = self.provider.build_payload(
            [
                Message(role="system", content="规则"),
                Message(role="user", content="核心记忆块"),
                Message(role="user", content="真实提问"),
            ]
        )
        self.assertEqual(len(payload["messages"]), 1)
        self.assertEqual(payload["messages"][0]["content"], "核心记忆块\n\n真实提问")

    def test_image_message_after_a_text_message_merges_into_blocks(self) -> None:
        payload = self.provider.build_payload(
            [
                Message(role="user", content="核心记忆块"),
                Message(role="user", content="看图", images=[self.make_part()]),
            ]
        )
        self.assertEqual(len(payload["messages"]), 1)
        blocks = payload["messages"][0]["content"]
        self.assertEqual([block["type"] for block in blocks], ["text", "text", "image"])

    def test_media_type_falls_back_to_the_extension(self) -> None:
        part = self.make_part()
        part.mime = ""
        payload = self.provider.build_payload(
            [Message(role="user", content="", images=[part])]
        )
        self.assertEqual(
            payload["messages"][0]["content"][0]["source"]["media_type"], "image/png"
        )


class ProviderAgreementTests(VisionCase):
    """形状不同、**取舍一致**：同一组用例同时喂给两个 provider。"""

    def test_both_providers_make_the_same_decisions(self) -> None:
        openai = OpenAICompatProvider(self.settings)
        anthropic = AnthropicCompatProvider(self.settings)
        cases = [
            Message(role="user", content="带文本和一张图", images=[self.make_part()]),
            Message(role="user", content="", images=[self.make_part(NOTE_B)]),
        ]
        for message in cases:
            with self.subTest(message=message.content):
                openai_blocks = openai.build_payload([message])["messages"][0]["content"]
                anthropic_blocks = anthropic.build_payload([message])["messages"][0]["content"]
                # 块数一样；文本块的有无也是一致的
                self.assertEqual(len(openai_blocks), len(anthropic_blocks))
                self.assertEqual(
                    [block["type"] == "text" for block in openai_blocks],
                    [block["type"] == "text" for block in anthropic_blocks],
                )
                self.assertEqual(openai_blocks[0]["type"] == "text", bool(message.content))


# --------------------------------------------------------------------------
# prompt_built 脱敏
# --------------------------------------------------------------------------


class PromptRecordTests(VisionCase):
    def test_prompt_record_drops_the_image_bytes_but_keeps_the_shape(self) -> None:
        message = Message(role="user", content="这是什么动物", images=[self.make_part()])
        record = message.to_prompt_record()
        self.assertEqual(record["role"], "user")
        self.assertEqual(record["content"], "这是什么动物")
        self.assertEqual(
            record["attachments"],
            [
                {
                    "id": NOTE_A,
                    "name": "image.png",
                    "mime": "image/png",
                    "width": 1300,
                    "height": 783,
                }
            ],
        )
        blob = json.dumps(record, ensure_ascii=False)
        self.assertNotIn("base64", blob)
        self.assertNotIn("data:image/", blob)

    def test_prompt_record_does_not_touch_the_disk_for_images(self) -> None:
        """脱敏路径**不许**为了"删掉 base64"而先把图读一遍 —— 文件故意不存在。"""
        ghost = ImagePart(
            path=self.tmp / "attachments" / SESSION / f"{NOTE_A}.png",
            mime="image/png",
            name="image.png",
            width=1300,
            height=783,
            id=NOTE_A,
        )
        record = Message(role="user", content="只有形状", images=[ghost]).to_prompt_record()
        self.assertEqual(record["content"], "只有形状")

    def test_non_user_messages_fall_back_to_to_api(self) -> None:
        message = Message(role="assistant", content="好的")
        self.assertEqual(message.to_prompt_record(), message.to_api())


# --------------------------------------------------------------------------
# 走一整轮 AgentLoop：日志里绝不能有 base64
# --------------------------------------------------------------------------


class LoopVisionTests(VisionCase):
    def harness(self, script, **overrides):
        built = make_harness(self.tmp, script, **overrides)
        self.addCleanup(built.close)
        return built

    def seed_attachment(self, note_id: str = NOTE_A, session_id: str = "test-session") -> dict:
        path = self.tmp / "attachments" / session_id / f"{note_id}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(TINY_PNG)
        return self.record(note_id, session_id=session_id)

    def test_user_message_event_keeps_the_attachment_record(self) -> None:
        harness = self.harness([text_reply("我看到一只猫")])
        attachment = self.seed_attachment()
        harness.loop.run("这是什么动物", attachments=[attachment])

        record = next(
            item for item in harness.log.read_events() if item["kind"] == "user_message"
        )
        self.assertEqual(record["text"], "这是什么动物")
        self.assertEqual(record["attachments"], [attachment])

    def test_no_attachment_field_when_there_are_no_images(self) -> None:
        """老形状不许变：没有附件时连这个键都不该出现（"没有"只有一种表示）。"""
        harness = self.harness([text_reply("好")])
        harness.loop.run("你好")
        record = next(
            item for item in harness.log.read_events() if item["kind"] == "user_message"
        )
        self.assertNotIn("attachments", record)

    def test_images_actually_reach_the_model(self) -> None:
        harness = self.harness([text_reply("看到了")])
        harness.loop.run("这是什么动物", attachments=[self.seed_attachment()])
        payloads = harness.provider.calls[0]["payloads"]
        user = next(item for item in payloads if item["role"] == "user")
        self.assertIsInstance(user["content"], list)
        self.assertIn("data:image/png;base64,", json.dumps(user, ensure_ascii=False))

    def test_no_base64_anywhere_in_the_session_log(self) -> None:
        """**硬断言**：会话 JSONL 的每一行都不许出现 base64 或 data URL。

        prompt_built 每一步都写一次；漏一次，日志就再也不能 grep、不能 --replay，
        data/ 也会迅速膨胀（契约 §5）。
        """
        harness = self.harness([text_reply("图一"), text_reply("图二")])
        attachment = self.seed_attachment()
        harness.loop.run("这是什么动物", attachments=[attachment])
        harness.loop.run("再描述一次", attachments=[self.seed_attachment(NOTE_B)])

        lines = harness.log.path.read_text(encoding="utf-8").splitlines()
        self.assertGreater(len(lines), 4, "至少要有两轮的 user/assistant/prompt 事件")
        for number, line in enumerate(lines, 1):
            self.assertNotIn("base64,", line, f"第 {number} 行泄漏了 base64：{line[:120]}")
            self.assertNotIn("data:image/", line, f"第 {number} 行泄漏了 data URL：{line[:120]}")

        prompts = harness.log.prompts()
        self.assertTrue(prompts)
        user_records = [
            message
            for prompt in prompts
            for message in prompt["messages"]
            if message.get("role") == "user" and message.get("attachments")
        ]
        self.assertTrue(user_records, "prompt_built 里必须留下「发过图」的形状记录")
        self.assertEqual(user_records[0]["attachments"][0]["id"], NOTE_A)

    def test_history_replay_keeps_the_images_on_the_second_turn(self) -> None:
        """用户在历史里发的图，之后每一轮都要**重新带上**（全部重发）。"""
        harness = self.harness([text_reply("第一次"), text_reply("第二次")])
        harness.loop.run("这是什么动物", attachments=[self.seed_attachment()])
        harness.loop.run("再说一次")

        replayed = harness.provider.calls[1]["messages"]
        first_user = next(message for message in replayed if message.role == "user")
        self.assertEqual(len(first_user.images), 1)
        self.assertEqual(first_user.images[0].id, NOTE_A)

        payloads = harness.provider.calls[1]["payloads"]
        first_payload = next(item for item in payloads if item["role"] == "user")
        self.assertIsInstance(first_payload["content"], list)
        self.assertIn("data:image/png;base64,", json.dumps(first_payload, ensure_ascii=False))

    def test_image_only_turn_round_trips(self) -> None:
        """只贴图不说话：文本为空但图片必须完整走到模型，日志里也不出现 base64。"""
        harness = self.harness([text_reply("这是一只猫")])
        harness.loop.run("", attachments=[self.seed_attachment()])

        user = next(
            item for item in harness.provider.calls[0]["payloads"] if item["role"] == "user"
        )
        self.assertEqual([block["type"] for block in user["content"]], ["image_url"])
        text = harness.log.path.read_text(encoding="utf-8")
        self.assertNotIn("base64,", text)

    def test_re_opening_the_log_restores_the_images(self) -> None:
        """换个进程按路径重新打开会话（data_dir 靠约定推导）也要能读出图。"""
        harness = self.harness([text_reply("好")])
        harness.loop.run("看图", attachments=[self.seed_attachment()])
        path = harness.log.path
        harness.close()
        harness.log = EventLog(path, "test-session")  # type: ignore[assignment]
        self.addCleanup(harness.log.close)

        messages = harness.log.conversation()
        self.assertEqual(len(messages[0].images), 1)
        self.assertEqual(messages[0].images[0].width, 1300)


# --------------------------------------------------------------------------
# 上下文与预算
# --------------------------------------------------------------------------


class EstimateTests(unittest.TestCase):
    def test_matches_the_measured_anchors(self) -> None:
        """公式的锚点来自真实调用（契约 §8）：750×503 ≈ 245、6 MP 到上限 1024。"""
        self.assertIn(estimate_image_tokens(750, 503), (245, 246))
        self.assertEqual(estimate_image_tokens(3000, 2012), 1024)
        # 小图会被官方放大到约 544×544，所以有个下限
        self.assertEqual(estimate_image_tokens(200, 200), 193)
        self.assertEqual(estimate_image_tokens(0, 0), 193)

    def test_image_only_message_costs_tokens(self) -> None:
        part = ImagePart(path=Path("image.png"), width=1300, height=1300)
        with_image = Message(role="user", content="", images=[part])
        without = Message(role="user", content="")
        self.assertGreater(estimate_message_tokens(with_image), 0)
        self.assertGreater(estimate_message_tokens(with_image), estimate_message_tokens(without))
        self.assertEqual(estimate_messages_tokens([with_image]), estimate_message_tokens(with_image))

    def test_more_pixels_cost_more_tokens(self) -> None:
        small = Message(role="user", content="", images=[ImagePart(path=Path("a"), width=600, height=600)])
        large = Message(role="user", content="", images=[ImagePart(path=Path("b"), width=2000, height=2000)])
        self.assertLess(estimate_message_tokens(small), estimate_message_tokens(large))


class SummaryRenderTests(unittest.TestCase):
    def test_image_marker_stays_in_the_summary_input(self) -> None:
        from harness.context import render_for_summary

        text = render_for_summary(
            [
                Message(
                    role="user",
                    content="这是什么动物",
                    images=[ImagePart(path=Path("image.png"), name="image.png", width=1300, height=783)],
                )
            ]
        )
        self.assertIn("用户：[图片 image.png 1300×783] 这是什么动物", text)

    def test_image_only_message_does_not_render_as_an_empty_line(self) -> None:
        from harness.context import render_for_summary

        text = render_for_summary(
            [Message(role="user", content="", images=[ImagePart(path=Path("x.png"), name="x.png")])]
        )
        self.assertIn("[图片 x.png", text)

    def test_text_only_rendering_is_unchanged(self) -> None:
        from harness.context import render_for_summary

        self.assertEqual(render_for_summary([Message(role="user", content="你好")]), "用户：你好")


class TitleTests(VisionCase):
    def test_image_only_first_message_falls_back_to_a_label(self) -> None:
        log = EventLog(self.settings.sessions_dir / "s1.jsonl", "s1")
        self.addCleanup(log.close)
        log.append("user_message", text="", attachments=[self.record()])
        self.assertEqual(session_display_title(log.path), "(图片)")

    def test_text_only_first_message_is_unchanged(self) -> None:
        log = EventLog(self.settings.sessions_dir / "s2.jsonl", "s2")
        self.addCleanup(log.close)
        log.append("user_message", text="你好世界", attachments=[self.record()])
        self.assertEqual(session_display_title(log.path), "你好世界")


class DataDirInferenceTests(VisionCase):
    def test_event_log_infers_the_data_dir_from_the_sessions_layout(self) -> None:
        """会话日志的约定位置是 <data_dir>/sessions/<sid>.jsonl，往上两级就是 data_dir。"""
        log = EventLog(self.settings.sessions_dir / "s9.jsonl", "s9")
        self.addCleanup(log.close)
        self.assertEqual(log.data_dir, self.tmp)

    def test_explicit_data_dir_wins(self) -> None:
        other = self.tmp / "elsewhere"
        log = EventLog(self.settings.sessions_dir / "s9.jsonl", "s9", other)
        self.addCleanup(log.close)
        self.assertEqual(log.data_dir, other)


class ConfigFieldTests(unittest.TestCase):
    def test_defaults_match_the_contract(self) -> None:
        settings = Settings.load()
        self.assertEqual(settings.attachment_max_bytes, 16777216)
        self.assertEqual(settings.attachment_max_count, 8)
        self.assertEqual(settings.attachment_max_pixels, 1690000)
        self.assertEqual(settings.attachment_max_side, 8192)
        self.assertEqual(settings.attachment_stored_max_bytes, 8388608)

    def test_images_use_the_shortest_whitelist(self) -> None:
        self.assertNotIn("svg", attachments.ALLOWED_EXTS)
        self.assertNotIn("html", attachments.ALLOWED_EXTS)


if __name__ == "__main__":
    unittest.main()
