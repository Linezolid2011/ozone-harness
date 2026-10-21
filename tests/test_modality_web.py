"""「多模态」开关 + 本地 OCR 降级（契约 docs/11-attachments.md §13）。

这一层要证明四件事：

* **开关**（§13.1）：`multimodal` 是会话级的、与会话绑定、**缺键 = 开**，
  SSE 的 `tools` 帧与 `build_session_view().tools` 都要回它；
* **发送前的闸**（§13.8）：档案声明/内置已知表/未知三层规则，
  判成"不支持"时**在写任何事件之前** 400 拒绝（`need: "vision"`）；
* **OCR 时机**（§13.2）：只有多模态**关着**才碰 OCR；开着时一次都不许调
  （这条专门防"开了多模态还白跑 OCR"）；引擎不可用 → 400 + `need: "ocr"` + 不写事件；
* **历史里的图片**（§13.8 末条）：这一条没带附件、但历史里有图、而模型被判"不支持"
  → 照常发送，只多一条 notice；模型支持或未知时**不发**（别没事吓唬人）。

写法照抄 tests/test_web_attachments.py：真起一个 `HarnessServer`，
假 provider 走真的 Runtime/AgentLoop，**不联网、不花钱**。
OCR 一律用假引擎（mock）—— 真引擎单张要 1.5~4 秒且依赖本机装没装，
用它会让这套测试又慢又飘。附件记录用 mock 顶掉 `records_for`，
所以这些用例**不需要 Pillow**；真上传的那条单独用 `needs_pillow` 标出来。
"""

from __future__ import annotations

import base64
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest import mock

from harness import attachments, web
from harness.events import EventLog, read_events
from harness.providers.store import (
    ProviderProfile,
    ProviderStore,
    get_preset,
    vision_support,
    vision_supported,
)
from harness.runtime import resolve_provider
from harness.web import HarnessHandler, HarnessServer, build_session_view

from .helpers import FakeProvider, seed_provider, test_settings, text_reply

HAS_PILLOW = attachments.pillow_available()
needs_pillow = unittest.skipUnless(
    HAS_PILLOW, "这条用例需要 Pillow（用 .venv 里的解释器跑才有）"
)

# 一张真的 1×1 PNG：**不需要 Pillow 就能构造**。测试里它只是"磁盘上确实有一个文件"，
# 真正的图片解码是 attachments 层的职责（那层另有自己的测试）。
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
HEX_ID = "a1b2c3d4e5f60718"
HEX_ID_2 = "b1b2c3d4e5f60718"


def make_png(width: int = 40, height: int = 30) -> bytes:
    """用 Pillow 造一张真图（只在 needs_pillow 的用例里调用）。"""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (10, 120, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeOcr:
    """假的本地 OCR 引擎。**签名与 harness.ocr 完全一致**（契约 §13.2 冻结的那份）。

    `web._load_ocr` 是这层唯一与引擎相接的接缝，所以整个测试只要替换它，
    就既能断言"到底调没调 OCR"，又不用把真引擎的 1.5~4 秒/张带进测试。
    """

    class OcrUnavailable(RuntimeError):
        pass

    def __init__(self, texts: list[str] | None = None, *, unavailable: bool = False):
        self.texts = list(texts if texts is not None else ["识别出来的文字"])
        self.unavailable = unavailable
        self.calls: list[dict] = []

    def fill_records(self, records, *, data_dir, emit=None):
        self.calls.append({"records": list(records), "data_dir": Path(data_dir)})
        if self.unavailable:
            raise self.OcrUnavailable("本地 OCR 引擎不可用（没有装 rapidocr-onnxruntime）")
        filled = []
        for index, record in enumerate(records):
            if emit is not None:
                emit("notice", level="info", text="正在识别图片文字…")
            text = self.texts[index] if index < len(self.texts) else ""
            item = dict(record)
            item["ocr"] = {
                "engine": "fake-ocr 0",
                "seconds": 0.01,
                "chars": len(text),
                "text": text,
            }
            filled.append(item)
        return filled


class WebCase(unittest.TestCase):
    """一个真在监听的服务 + 一个临时数据目录（照抄 test_web_attachments.py）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.settings = test_settings(self.tmp / "data", request_timeout=1.0)
        # 接口是"用户自己加的"；base_url 指向连不通的端口，保证绝不联网
        seed_provider(self.settings, base_url="http://127.0.0.1:1")
        self.server = HarnessServer(("127.0.0.1", 0), HarnessHandler)
        self.server.settings = self.settings
        self.server.verbose = False
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    # -- HTTP 小工具 --------------------------------------------------

    def _post(self, path: str, payload: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return exc.code, {}

    def _get(self, path: str) -> tuple[int, dict]:
        request = urllib.request.Request(self.base + path, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")

    def _sse(self, path: str, payload: dict) -> tuple[int, str]:
        """POST 一个 SSE 接口，**把 400 也当普通结果返回**（闸的用例要看它）。"""
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def _chat(self, payload: dict, replies: list[str] | None = None):
        """走真的 Runtime/AgentLoop，只把 provider 换成脚本化的假货。

        只替换**产物**里的 provider（`dataclasses.replace`），`resolve_provider`
        本身照常按请求里的 provider/model/effort 重新解析 —— 否则"换个模型再发一次"
        这类断言会因为 patch 把入参吃掉而变成假绿。
        """
        fake = FakeProvider([text_reply(text) for text in (replies or ["好的"])])
        real = resolve_provider

        def resolve_with_fake(settings, **kwargs):
            return replace(real(settings, **kwargs), provider=fake)

        with mock.patch.object(web, "resolve_provider", resolve_with_fake):
            status, body = self._sse("/api/chat", payload)
        return fake, status, body

    # -- 附件小工具 ---------------------------------------------------

    def _attachment_record(self, session_id: str, *, note_id: str = HEX_ID) -> dict:
        """在磁盘上放一个真文件，并造出 `records_for` 会返回的那种记录。"""
        relative = f"attachments/{session_id}/{note_id}.png"
        path = self.settings.data_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(TINY_PNG)
        return {
            "id": note_id,
            "file": relative,
            "name": "cat.png",
            "mime": "image/png",
            "bytes": len(TINY_PNG),
            "width": 1,
            "height": 1,
            "stored_bytes": len(TINY_PNG),
            "stored_width": 1,
            "stored_height": 1,
        }

    @staticmethod
    def _wanted(note_id: str = HEX_ID) -> list[dict]:
        return [{"id": note_id, "ext": "png", "name": "cat.png"}]

    def _patch_records(self, *records: dict):
        """顶掉 `records_for`：闸与 OCR 只关心"有没有附件记录"，不关心怎么解析出来的。"""
        return mock.patch.object(
            attachments, "records_for", lambda settings, session_id, wanted: list(records)
        )

    def _sessions_snapshot(self) -> dict[str, bytes]:
        root = self.settings.sessions_dir
        if not root.exists():
            return {}
        return {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    def _session_path(self, session_id: str) -> Path:
        return self.settings.sessions_dir / f"{session_id}.jsonl"

    def _user_event(self, session_id: str) -> dict:
        events = read_events(self._session_path(session_id))
        return next(item for item in events if item["kind"] == "user_message")

    def _make_log(self, session_id: str, *appends) -> Path:
        log = EventLog(
            self._session_path(session_id), session_id, data_dir=self.settings.data_dir
        )
        for kind, data in appends:
            log.append(kind, **data)
        log.close()
        return self._session_path(session_id)

    def _set_vision_models(self, models: list[str] | None) -> None:
        """改这个档案的「支持图片的模型」名单。`None` = 清空 = 不声明 = 未知。"""
        store = ProviderStore(self.settings.data_dir / "providers.json")
        profile = store.get("deepseek")
        assert profile is not None
        profile.vision_models = list(models or [])
        store.upsert(profile)

    def _tools_frame(self, body: str) -> dict:
        return [
            json.loads(line[6:])
            for line in body.split("\n")
            if line.startswith("data: ") and '"multimodal"' in line
        ][0]


# --------------------------------------------------------------------------
# 发送前的闸（契约 §13.8）
# --------------------------------------------------------------------------


class VisionGateTests(WebCase):
    def test_declared_whitelist_rejects_a_model_outside_it(self) -> None:
        """档案**有**名单 = 白名单，名单外的模型一律按不支持处理。"""
        self._set_vision_models(["deepseek-flash"])
        self._make_log("s-gate", ("user_message", {"text": "你好"}))
        before = self._sessions_snapshot()
        record = self._attachment_record("s-gate")

        with self._patch_records(record):
            fake, status, body = self._chat(
                {
                    "session_id": "s-gate",
                    "text": "这是什么动物",
                    "model": "deepseek-v4-pro",
                    "attachments": self._wanted(),
                }
            )

        self.assertEqual(status, 400, body)
        payload = json.loads(body)
        self.assertEqual(payload["need"], "vision")
        self.assertIn("deepseek-v4-pro", payload["error"])
        self.assertIn("档案声明", payload["error"])
        # 三条出路：关多模态 / 换模型 / 自己去档案里声明
        self.assertIn("多模态", payload["error"])
        self.assertIn("设置 → 接口", payload["error"])
        # **在写任何事件之前**：磁盘一个字节都没变（没有 user_message、
        # 没有 session_start、也没有 session_tools）
        self.assertEqual(self._sessions_snapshot(), before)
        self.assertEqual(fake.calls, [], "被闸拦下的请求绝不该碰到模型")

    def test_the_same_attachment_passes_with_a_declared_vision_model(self) -> None:
        self._set_vision_models(["deepseek-flash"])
        record = self._attachment_record("s-ok")

        with self._patch_records(record):
            fake, status, body = self._chat(
                {
                    "session_id": "s-ok",
                    "text": "这是什么动物",
                    "model": "deepseek-flash",
                    "attachments": self._wanted(),
                }
            )

        self.assertEqual(status, 200, body[:300])
        self.assertIn("event: done", body)
        event = self._user_event("s-ok")
        self.assertEqual([item["id"] for item in event["attachments"]], [HEX_ID])
        self.assertTrue(fake.calls, "放行的请求要真的走到模型")

    def test_no_declaration_falls_back_to_the_builtin_table(self) -> None:
        """**没有**这个键 = 未知 → 查内置已知表；v4-pro 在那张表里是"不支持"。"""
        self._set_vision_models([])
        self._make_log("s-builtin", ("user_message", {"text": "你好"}))
        before = self._sessions_snapshot()
        record = self._attachment_record("s-builtin")

        with self._patch_records(record):
            _fake, status, body = self._chat(
                {
                    "session_id": "s-builtin",
                    "text": "看图",
                    "model": "deepseek-v4-pro",
                    "attachments": self._wanted(),
                }
            )

        self.assertEqual(status, 400, body)
        payload = json.loads(body)
        self.assertEqual(payload["need"], "vision")
        self.assertIn("内置已知表", payload["error"], "要告诉用户判定从哪来")
        self.assertEqual(self._sessions_snapshot(), before)

    def test_a_model_declared_in_the_profile_is_allowed_even_if_unknown_to_us(self) -> None:
        """档案声明了一个我们没听过的模型名：白名单说了算，**放行**。"""
        self._set_vision_models(["my-vision-9"])
        record = self._attachment_record("s-custom")

        with self._patch_records(record):
            _fake, status, body = self._chat(
                {
                    "session_id": "s-custom",
                    "text": "看图",
                    "model": "my-vision-9",
                    "attachments": self._wanted(),
                }
            )

        self.assertEqual(status, 200, body[:300])
        self.assertIn("event: done", body)

    def test_an_unknown_model_without_a_declaration_is_sent(self) -> None:
        """查不到 = `None` = **未知**（不是"不支持"）：照发，真报错走 §13.5。"""
        self._set_vision_models([])
        record = self._attachment_record("s-unknown")

        with self._patch_records(record):
            _fake, status, body = self._chat(
                {
                    "session_id": "s-unknown",
                    "text": "看图",
                    "model": "mystery-model-1",
                    "attachments": self._wanted(),
                }
            )

        self.assertEqual(status, 200, body[:300])


class VisionSupportUnitTests(unittest.TestCase):
    """判定函数本身（闸与历史提醒共用的**唯一**实现）。"""

    def _profile(self, models: list[str] | None = None) -> ProviderProfile:
        """`models=None` = 用预设原样（DeepSeek 预设声明了 flash）。"""
        profile = ProviderProfile.from_preset(get_preset("deepseek"))
        if models is not None:
            profile.vision_models = list(models)
        return profile

    def test_declared_whitelist_wins(self) -> None:
        profile = self._profile(["a", "b"])
        self.assertEqual(vision_support(profile, "a"), (True, "profile"))
        self.assertEqual(vision_support(profile, "c"), (False, "profile"))

    def test_builtin_table_is_used_only_without_a_declaration(self) -> None:
        profile = self._profile([])
        self.assertEqual(vision_supported(profile, "deepseek-flash"), True)
        self.assertEqual(vision_supported(profile, "deepseek-v4-pro"), False)
        self.assertIsNone(vision_supported(profile, "谁都没听过"))
        # 空 = 未知 = 照发；而"声明了但为空"读取时就被归一成"没声明"
        self.assertEqual(vision_support(profile, "谁都没听过")[1], "unknown")

    def test_the_deepseek_preset_declares_only_flash(self) -> None:
        profile = self._profile()
        self.assertEqual(profile.vision_models, ["deepseek-flash"])
        self.assertNotIn("deepseek-v4-pro", profile.vision_models)

    def test_public_state_and_storage_carry_the_field(self) -> None:
        """前端要能读能写：`public_state()` 里有它，存盘再读回来也不能丢。"""
        profile = self._profile(["deepseek-flash"])
        self.assertEqual(profile.to_public()["vision_models"], ["deepseek-flash"])
        self.assertEqual(
            ProviderProfile.from_dict(profile.to_storage()).vision_models,
            ["deepseek-flash"],
        )
        # 老档案（没有这个键）= 未知，不是"谁都不支持"
        self.assertEqual(ProviderProfile.from_dict({"id": "x"}).vision_models, [])


class VisionModelsApiTests(WebCase):
    """前端要能**读能写**这个名单（否则内置表判错时用户没有纠正手段）。"""

    def test_save_and_read_back_through_the_http_api(self) -> None:
        status, result = self._post(
            "/api/providers/save",
            {"id": "deepseek", "vision_models": ["deepseek-flash", "my-vision-9"]},
        )
        self.assertEqual(status, 200, result)
        profile = next(
            item for item in result["state"]["providers"] if item["id"] == "deepseek"
        )
        self.assertEqual(profile["vision_models"], ["deepseek-flash", "my-vision-9"])

        # GET 之后仍在（刷新页面，编辑框里要有值）
        status, state = self._get("/api/providers")
        self.assertEqual(status, 200)
        profile = next(
            item for item in state["providers"] if item["id"] == "deepseek"
        )
        self.assertEqual(profile["vision_models"], ["deepseek-flash", "my-vision-9"])

    def test_clearing_it_means_unknown_not_unsupported(self) -> None:
        """留空 = 未知（照发），**不是**"谁都不支持"。"""
        status, _result = self._post(
            "/api/providers/save", {"id": "deepseek", "vision_models": ""}
        )
        self.assertEqual(status, 200)
        _status, state = self._get("/api/providers")
        profile = next(
            item for item in state["providers"] if item["id"] == "deepseek"
        )
        self.assertEqual(profile["vision_models"], [])

        record = self._attachment_record("s-cleared")
        with self._patch_records(record):
            _fake, status, body = self._chat(
                {
                    "session_id": "s-cleared",
                    "text": "看图",
                    "model": "mystery-model-1",
                    "attachments": self._wanted(),
                }
            )
        self.assertEqual(status, 200, body[:300])


# --------------------------------------------------------------------------
# OCR 时机（契约 §13.2）
# --------------------------------------------------------------------------


class OcrPathTests(WebCase):
    def test_modality_off_runs_ocr_and_sends_the_text(self) -> None:
        ocr = FakeOcr(["第一行\n第二行"])
        record = self._attachment_record("s-ocr")
        with self._patch_records(record), mock.patch.object(
            web, "_load_ocr", lambda: ocr
        ):
            _fake, status, body = self._chat(
                {
                    "session_id": "s-ocr",
                    "text": "这张图里写了什么？",
                    "multimodal": False,
                    "attachments": self._wanted(),
                },
                replies=["看到了两行字"],
            )

        self.assertEqual(status, 200, body[:300])
        self.assertEqual(len(ocr.calls), 1, "关着多模态必须走 OCR")
        self.assertEqual(ocr.calls[0]["data_dir"], self.settings.data_dir)

        # 1) 事件里记下了 modality="text" 与 ocr（§13.3）
        event = self._user_event("s-ocr")
        self.assertEqual(event["modality"], "text")
        self.assertEqual(event["attachments"][0]["ocr"]["text"], "第一行\n第二行")
        self.assertEqual(event["attachments"][0]["ocr"]["engine"], "fake-ocr 0")

        # 2) 汇总 notice：人话 + 张数，而且**排在模型输出之前**（§13.7）
        self.assertIn("多模态模式已关", body)
        self.assertIn("已把 1 张图片 OCR 成文字发给模型", body)
        self.assertLess(
            body.index("多模态模式已关"),
            body.index("看到了两行字"),
            "用户要先看到「发生了什么」，再看到模型的流",
        )

    def test_modality_on_never_touches_ocr(self) -> None:
        """开着多模态还去 OCR = 白烧 CPU 与时间（单张 1.5~3.4s）。一次都不许调。"""
        ocr = FakeOcr()
        record = self._attachment_record("s-noocr")
        with self._patch_records(record), mock.patch.object(
            web, "_load_ocr", lambda: ocr
        ):
            _fake, status, body = self._chat(
                {
                    "session_id": "s-noocr",
                    "text": "看图",
                    "multimodal": True,
                    "attachments": self._wanted(),
                }
            )

        self.assertEqual(status, 200, body[:300])
        self.assertEqual(ocr.calls, [], "开了多模态不该调用 OCR")
        self.assertNotIn("多模态模式已关", body)
        event = self._user_event("s-noocr")
        self.assertEqual(event["modality"], "image")
        self.assertNotIn("ocr", event["attachments"][0])

    def test_missing_key_defaults_to_image_and_skips_ocr(self) -> None:
        """缺键 = 开（§13.1）：老前端送来的请求行为一个字都不能变。"""
        ocr = FakeOcr()
        record = self._attachment_record("s-default")
        with self._patch_records(record), mock.patch.object(
            web, "_load_ocr", lambda: ocr
        ):
            _fake, status, body = self._chat(
                {
                    "session_id": "s-default",
                    "text": "看图",
                    "attachments": self._wanted(),
                }
            )

        self.assertEqual(status, 200, body[:300])
        self.assertEqual(ocr.calls, [])
        self.assertEqual(self._user_event("s-default")["modality"], "image")

    def test_a_summary_notice_counts_the_images_without_text(self) -> None:
        ocr = FakeOcr(["有字", "   "])
        first = self._attachment_record("s-mix", note_id=HEX_ID)
        second = self._attachment_record("s-mix", note_id=HEX_ID_2)
        with self._patch_records(first, second), mock.patch.object(
            web, "_load_ocr", lambda: ocr
        ):
            _fake, status, body = self._chat(
                {
                    "session_id": "s-mix",
                    "text": "看图",
                    "multimodal": False,
                    "attachments": self._wanted(),
                }
            )

        self.assertEqual(status, 200, body[:300])
        self.assertIn("已把 2 张图片 OCR 成文字发给模型", body)
        self.assertIn("其中 1 张没有识别到任何文字", body)
        self.assertIn("打开「多模态」", body)

    def test_ocr_unavailable_is_a_400_with_need_and_writes_nothing(self) -> None:
        ocr = FakeOcr(unavailable=True)
        self._make_log("s-noengine", ("user_message", {"text": "你好"}))
        before = self._sessions_snapshot()
        record = self._attachment_record("s-noengine")

        with self._patch_records(record), mock.patch.object(
            web, "_load_ocr", lambda: ocr
        ):
            fake, status, body = self._chat(
                {
                    "session_id": "s-noengine",
                    "text": "看图",
                    "multimodal": False,
                    "attachments": self._wanted(),
                }
            )

        self.assertEqual(status, 400, body)
        payload = json.loads(body)
        self.assertEqual(payload["need"], "ocr")
        # 两条出路都要写出来（契约 §13.2）
        self.assertIn("run.cmd --setup", payload["error"])
        self.assertIn("多模态", payload["error"])
        # **不留半截状态**：没有新事件，也没碰到模型
        self.assertEqual(self._sessions_snapshot(), before)
        self.assertEqual(fake.calls, [])
        # 而且一帧 SSE 都没发出去（否则就回不了这个 400 了）
        self.assertNotIn("event:", body)

    def test_the_engine_is_loaded_lazily(self) -> None:
        """`web` 模块自己**不 import** 引擎：无依赖模式下 Web 端必须能起来。"""
        self.assertFalse(
            hasattr(web, "ocr"),
            "web 顶层不许 import ocr —— 引擎缺失时整个服务会起不来",
        )
        self.assertTrue(callable(web._load_ocr))


# --------------------------------------------------------------------------
# 开关的持久化与会话视图（契约 §13.1 / §13.3）
# --------------------------------------------------------------------------


class SwitchPersistenceTests(WebCase):
    def test_chat_persists_the_switch_and_the_tools_frame_carries_it(self) -> None:
        with mock.patch.object(web, "_load_ocr", lambda: FakeOcr()):
            _fake, status, body = self._chat(
                {"session_id": "s-sw", "text": "你好", "multimodal": False}
            )
        self.assertEqual(status, 200, body[:200])

        frame = self._tools_frame(body)
        self.assertFalse(frame["multimodal"])
        self.assertIn("web_search", frame, "两个开关必须在同一帧里回")

        # 与会话绑定：落一条 session_tools 事件，**两个键一起写**
        record = next(
            item
            for item in read_events(self._session_path("s-sw"))
            if item["kind"] == "session_tools"
        )
        self.assertFalse(record["multimodal"])
        self.assertIn("web_search", record)

        # 刷新后读得回来
        view = build_session_view(self.settings, "s-sw")
        self.assertFalse(view["tools"]["multimodal"])
        # 第二次不带这个字段：沿用上次存的，且**不再追加**重复事件
        before = read_events(self._session_path("s-sw"))
        _fake, status, body = self._chat({"session_id": "s-sw", "text": "再来"})
        self.assertEqual(status, 200)
        self.assertFalse(self._tools_frame(body)["multimodal"])
        after = read_events(self._session_path("s-sw"))
        self.assertEqual(
            len([item for item in after if item["kind"] == "session_tools"]),
            len([item for item in before if item["kind"] == "session_tools"]),
            "开关没变就不该再写一条",
        )

    def test_an_old_session_without_the_key_is_on(self) -> None:
        """老会话（`session_tools` 里根本没有 multimodal）= **开**（§13.1）。"""
        self._make_log(
            "s-old",
            ("session_tools", {"web_search": True}),
            ("user_message", {"text": "你好"}),
        )
        view = build_session_view(self.settings, "s-old")
        self.assertTrue(view["tools"]["multimodal"])
        self.assertTrue(view["tools"]["web_search"], "老键一个都不能丢")

    def test_a_session_without_any_switch_event_is_on(self) -> None:
        self._make_log("s-none", ("user_message", {"text": "你好"}))
        self.assertTrue(build_session_view(self.settings, "s-none")["tools"]["multimodal"])

    def test_the_tools_endpoint_round_trips_multimodal(self) -> None:
        self._make_log("s-endpoint", ("user_message", {"text": "你好"}))
        status, result = self._post(
            "/api/sessions/s-endpoint/tools", {"multimodal": False}
        )
        self.assertEqual(status, 200, result)
        self.assertFalse(result["multimodal"])
        self.assertIn("web_search", result)
        self.assertFalse(
            build_session_view(self.settings, "s-endpoint")["tools"]["multimodal"]
        )
        # 顺手把联网搜索打开：两个键互不干扰（最后一条为准，各判各的）
        self._post("/api/sessions/s-endpoint/tools", {"web_search": True})
        tools = build_session_view(self.settings, "s-endpoint")["tools"]
        self.assertTrue(tools["web_search"])
        self.assertFalse(tools["multimodal"])


class SessionViewOcrTests(WebCase):
    def test_a_text_modality_message_carries_the_ocr_text(self) -> None:
        record = {
            "id": HEX_ID,
            "file": f"attachments/s-view/{HEX_ID}.png",
            "name": "image.png",
            "mime": "image/png",
            "bytes": 123,
            "width": 10,
            "height": 10,
            "stored_bytes": 123,
            "stored_width": 10,
            "stored_height": 10,
            "ocr": {
                "engine": "rapidocr-onnxruntime 1.2.3",
                "seconds": 1.57,
                "chars": 7,
                "text": "第一行\n第二行",
            },
        }
        (self.settings.data_dir / record["file"]).parent.mkdir(parents=True, exist_ok=True)
        (self.settings.data_dir / record["file"]).write_bytes(TINY_PNG)
        self._make_log(
            "s-view",
            ("user_message", {"text": "看图", "modality": "text", "attachments": [record]}),
        )

        message = build_session_view(self.settings, "s-view")["messages"][0]
        self.assertEqual(message["modality"], "text")
        attachment = message["attachments"][0]
        self.assertEqual(attachment["ocr"]["text"], "第一行\n第二行")
        self.assertEqual(attachment["ocr"]["engine"], "rapidocr-onnxruntime 1.2.3")
        self.assertEqual(attachment["ocr"]["chars"], 7)
        self.assertIn("url", attachment)

    def test_an_old_message_has_no_modality_key(self) -> None:
        """缺键 = image：老日志的视图形状**一个字都不变**。"""
        self._make_log("s-plain", ("user_message", {"text": "你好"}))
        message = build_session_view(self.settings, "s-plain")["messages"][0]
        self.assertNotIn("modality", message)
        self.assertNotIn("attachments", message)


# --------------------------------------------------------------------------
# 历史里的图片（契约 §13.8 末条）
# --------------------------------------------------------------------------


class HistoryImageNoticeTests(WebCase):
    def _history_with_one_image(self, session_id: str) -> None:
        record = self._attachment_record(session_id)
        self._make_log(
            session_id,
            ("user_message", {"text": "这是什么动物", "attachments": [record]}),
        )

    def test_a_visionless_model_gets_warned_when_history_has_images(self) -> None:
        self._set_vision_models(["deepseek-flash"])
        self._history_with_one_image("s-warn")

        _fake, status, body = self._chat(
            {"session_id": "s-warn", "text": "把上面聊的总结一下", "model": "deepseek-v4-pro"}
        )

        self.assertEqual(
            status, 200, f"这是提醒，不是拒绝：{status} {body[:300]}"
        )
        self.assertIn("event: notice", body)
        self.assertIn("会话前面有 1 张图片", body)
        self.assertIn("deepseek-v4-pro", body)
        self.assertIn("event: done", body)

    def test_a_supported_model_is_not_warned(self) -> None:
        self._set_vision_models(["deepseek-flash"])
        self._history_with_one_image("s-quiet")

        _fake, status, body = self._chat(
            {"session_id": "s-quiet", "text": "总结一下", "model": "deepseek-flash"}
        )

        self.assertEqual(status, 200, body[:300])
        self.assertNotIn("会话前面有", body, "模型支持图片时不该吓唬人")

    def test_an_unknown_model_is_not_warned_either(self) -> None:
        """未知 = 照发：**不发**这条提醒（没有判据说它看不见图）。"""
        self._set_vision_models([])
        self._history_with_one_image("s-unknown-hist")

        _fake, status, body = self._chat(
            {
                "session_id": "s-unknown-hist",
                "text": "总结一下",
                "model": "mystery-model-1",
            }
        )

        self.assertEqual(status, 200, body[:300])
        self.assertNotIn("会话前面有", body)

    def test_no_history_images_means_no_notice(self) -> None:
        self._set_vision_models(["deepseek-flash"])
        self._make_log("s-noimg", ("user_message", {"text": "你好"}))
        _fake, status, body = self._chat(
            {"session_id": "s-noimg", "text": "继续", "model": "deepseek-v4-pro"}
        )
        self.assertEqual(status, 200, body[:300])
        self.assertNotIn("会话前面有", body)


# --------------------------------------------------------------------------
# 边界：没有附件时开关不该有任何副作用
# --------------------------------------------------------------------------


class EmptyAttachmentsTests(WebCase):
    def test_no_attachments_means_no_ocr_and_no_gate(self) -> None:
        ocr = FakeOcr()
        with mock.patch.object(web, "_load_ocr", lambda: ocr):
            # 多模态**关着**、模型也不支持图片：没有附件就都不该触发
            _fake, status, body = self._chat(
                {
                    "session_id": "s-empty",
                    "text": "只是聊聊",
                    "multimodal": False,
                    "model": "deepseek-v4-pro",
                }
            )
        self.assertEqual(status, 200, body[:300])
        self.assertEqual(ocr.calls, [], "没有附件不该碰 OCR")
        self.assertNotIn("多模态模式已关", body)
        event = self._user_event("s-empty")
        self.assertNotIn("modality", event, "没有附件时连这个键都不写")
        self.assertNotIn("attachments", event)

    def test_no_attachments_no_vision_refusal(self) -> None:
        """闸只管"附件存在 + 发图"；纯文字消息对着不支持的模型也必须能发。"""
        _fake, status, body = self._chat(
            {
                "session_id": "s-textonly",
                "text": "你好",
                "model": "deepseek-v4-pro",
                "attachments": [],
            }
        )
        self.assertEqual(status, 200, body[:300])


# --------------------------------------------------------------------------
# 真附件的端到端（需要 Pillow）：证明"记录来自磁盘"这条接缝也接得上
# --------------------------------------------------------------------------


class RealAttachmentOcrTests(WebCase):
    @needs_pillow
    def test_upload_then_ocr_chat_end_to_end(self) -> None:
        uploaded = self._post(
            "/api/attachments",
            {
                "session_id": None,
                "name": "cat.png",
                "mime": "image/png",
                "data_base64": base64.b64encode(make_png()).decode("ascii"),
            },
        )
        self.assertEqual(uploaded[0], 200, uploaded)
        session_id = uploaded[1]["session_id"]
        attachment = uploaded[1]["attachment"]

        ocr = FakeOcr(["猫"])
        with mock.patch.object(web, "_load_ocr", lambda: ocr):
            _fake, status, body = self._chat(
                {
                    "session_id": session_id,
                    "text": "图里是什么",
                    "multimodal": False,
                    "attachments": [
                        {"id": attachment["id"], "ext": "png", "name": "cat.png"}
                    ],
                }
            )

        self.assertEqual(status, 200, body[:300])
        event = self._user_event(session_id)
        self.assertEqual(event["modality"], "text")
        self.assertEqual(event["attachments"][0]["ocr"]["text"], "猫")
        view = build_session_view(self.settings, session_id)
        self.assertEqual(view["messages"][0]["modality"], "text")
        self.assertEqual(view["messages"][0]["attachments"][0]["ocr"]["text"], "猫")


if __name__ == "__main__":
    unittest.main()
