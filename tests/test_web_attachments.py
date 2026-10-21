"""图片附件的 HTTP 接口、会话生命周期与整理输入（契约 docs/11-attachments.md）。

覆盖四层：

* **上传 / 取图**（§7.1、§7.2）：形状、响应头、两道路径闸、以及"请求体太大时
  先看 Content-Length 再决定读不读"这条闸；
* **对话**（§7.3）：假 provider 真的收到了图片块，而会话 JSONL 里一个 base64
  都没有（§5 的硬规则）；前端给的路径/尺寸一律不算数；
* **生命周期**（§2 末、§10）：删会话连附件目录一起删、别的会话一张不少；
  重置预览必须报出图片数量；
* **整理输入**（§9）：只贴图不说话的消息不再凭空消失。

真起一个 `HarnessServer`（照抄 tests/test_web.py 的做法），**不联网、不花钱**。
Pillow 相关用例在"无依赖模式"（系统 Python）下 skip 而不是报错 ——
本项目的无依赖模式必须照样全绿（与 tests/test_attachments.py 同一规矩）。
"""

from __future__ import annotations

import base64
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest import mock

from harness import attachments, web
from harness.dream_records import render_event
from harness.events import EventLog, read_events
from harness.reset import preview_reset
from harness.runtime import resolve_provider
from harness.web import (
    HarnessHandler,
    HarnessServer,
    _attachment_body_limit,
    _session_path,
    attachment_url,
    build_session_view,
)

from .helpers import FakeProvider, seed_provider, test_settings, text_reply

HAS_PILLOW = attachments.pillow_available()
needs_pillow = unittest.skipUnless(
    HAS_PILLOW, "这条用例需要 Pillow（用 .venv 里的解释器跑才有）"
)

# 一张真的 1×1 PNG：**不需要 Pillow 就能构造**，用于"闸门/404"这类不需要解码的用例。
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
HEX_ID = "a1b2c3d4e5f60718"


def make_png(width: int = 60, height: int = 40) -> bytes:
    """用 Pillow 造一张真图（只在 needs_pillow 的用例里调用）。"""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (10, 120, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


class WebCase(unittest.TestCase):
    """一个真在监听的服务 + 一个临时数据目录。"""

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

    # -- HTTP 小工具（与 test_web.py 同一套）--------------------------

    def _post(self, path: str, payload: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._finish(request)

    def _post_raw(
        self, path: str, body: bytes, headers: dict[str, str]
    ) -> tuple[int, dict]:
        """自定义 body（用来造一个"请求体本身很大"的请求）。"""
        request = urllib.request.Request(
            self.base + path, data=body, headers=headers, method="POST"
        )
        return self._finish(request)

    def _finish(self, request: urllib.request.Request) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return exc.code, {}

    def _get_raw(self, path: str) -> tuple[int, bytes, dict[str, str]]:
        request = urllib.request.Request(self.base + path, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def _post_sse(self, path: str, payload: dict) -> tuple[int, str]:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as resp:
            return resp.status, resp.read().decode("utf-8")

    # -- 附件小工具 --------------------------------------------------

    def _upload(
        self,
        data: bytes,
        *,
        session_id: str = "",
        name: str = "cat.png",
        mime: str = "image/png",
    ) -> tuple[int, dict]:
        return self._post(
            "/api/attachments",
            {
                "session_id": session_id or None,
                "name": name,
                "mime": mime,
                "data_base64": base64.b64encode(data).decode("ascii"),
            },
        )

    def _snapshot(self, root: Path) -> dict[str, bytes]:
        if not root.exists():
            return {}
        return {
            str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()
        }

    def _chat_with_fake_provider(
        self, payload: dict, replies: list[str] | None = None
    ) -> tuple[FakeProvider, int, str]:
        """把 `/api/chat` 里的接口换成假 provider：真走 Runtime/AgentLoop，但不联网。

        只替换 `resolve_provider` 的**产物**（把 provider 换成 FakeProvider），
        Runtime 与 AgentLoop 都是真的 —— 这样断言的是"我们这个管道真的把图片
        发出去了、日志里真的没泄漏 base64"，而不是一个被 mock 掉的空壳。
        """
        fake = FakeProvider([text_reply(text) for text in (replies or ["好的"])])
        resolved = replace(resolve_provider(self.settings), provider=fake)
        with mock.patch.object(web, "resolve_provider", lambda *a, **k: resolved):
            status, body = self._post_sse("/api/chat", payload)
        return fake, status, body


# --------------------------------------------------------------------------
# 上传（契约 §7.1）
# --------------------------------------------------------------------------


class UploadTests(WebCase):
    @needs_pillow
    def test_upload_returns_the_contract_shape_and_writes_only_the_image(self) -> None:
        status, result = self._upload(make_png(80, 40), name="小猫.png")
        self.assertEqual(status, 200, result)
        self.assertTrue(result["ok"])
        self.assertEqual(result["warnings"], [])

        session_id = result["session_id"]
        # 新会话 id 的形状必须与 events.new_session_id() 一致（否则前端拿它去
        # 聊天时会被自己的白名单挡回来）
        self.assertRegex(session_id, r"^[0-9A-Za-z][0-9A-Za-z_-]{0,63}$")
        self.assertIsNotNone(_session_path(self.settings, session_id))

        attachment = result["attachment"]
        for key in (
            "id",
            "file",
            "name",
            "mime",
            "bytes",
            "width",
            "height",
            "stored_bytes",
            "stored_width",
            "stored_height",
            "url",
        ):
            self.assertIn(key, attachment, f"契约 §7.1 要求 {key}")
        self.assertEqual(attachment["name"], "小猫.png")
        self.assertEqual(attachment["mime"], "image/png")
        self.assertEqual((attachment["width"], attachment["height"]), (80, 40))

        # 文件真的落在 <data>/attachments/<sid>/ 下
        path = self.settings.data_dir / attachment["file"]
        self.assertTrue(path.is_file())
        self.assertTrue(
            path.is_relative_to(self.settings.data_dir / "attachments" / session_id)
        )
        # url 由服务端给出，且与唯一那处拼装一致
        self.assertEqual(
            attachment["url"], attachment_url(session_id, attachment["id"], "png")
        )

        # **不创建 .jsonl**：只是加了图、还没发出去，不该凭空多一个空会话
        self.assertFalse(
            (self.settings.sessions_dir / f"{session_id}.jsonl").exists(),
            "上传不该创建会话日志",
        )
        self.assertFalse(self.settings.sessions_dir.exists())

    @needs_pillow
    def test_upload_to_a_given_session_lands_in_that_session_directory(self) -> None:
        status, result = self._upload(make_png(), session_id="s-owner")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["session_id"], "s-owner")
        path = self.settings.data_dir / result["attachment"]["file"]
        self.assertEqual(
            path.parent, self.settings.data_dir / "attachments" / "s-owner"
        )

    def test_illegal_session_id_is_rejected_and_disk_is_untouched(self) -> None:
        before = self._snapshot(self.settings.data_dir)
        for bad in ("../evil", "../../x", "..", "a/b", "a\\b", "x" * 65, "CON", "sneaky.jsonl"):
            with self.subTest(session_id=bad):
                status, result = self._post(
                    "/api/attachments",
                    {
                        "session_id": bad,
                        "name": "x.png",
                        "mime": "image/png",
                        "data_base64": base64.b64encode(TINY_PNG).decode(),
                    },
                )
                self.assertEqual(status, 400, f"{bad!r} 应当被拒绝")
                self.assertIn("非法", result["error"])
        self.assertEqual(
            self._snapshot(self.settings.data_dir), before, "非法 id 不该碰磁盘"
        )

    def test_broken_base64_is_rejected_with_a_readable_message(self) -> None:
        for bad in ("this is not base64!!!", "@@@@"):
            with self.subTest(value=bad):
                status, result = self._post(
                    "/api/attachments",
                    {"name": "x.png", "mime": "image/png", "data_base64": bad},
                )
                self.assertEqual(status, 400)
                self.assertIn("base64", result["error"])

    @needs_pillow
    def test_text_pretending_to_be_a_png_is_rejected(self) -> None:
        """格式**按内容判断**：改扩展名 + 声明 image/png 都不是图片。"""
        status, result = self._post(
            "/api/attachments",
            {
                "name": "page.png",
                "mime": "image/png",
                "data_base64": base64.b64encode(
                    b"<!DOCTYPE html><html><script>x</script></html>"
                ).decode(),
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("内容", result["error"])
        self.assertFalse((self.settings.data_dir / "attachments").exists())

    def test_oversized_request_body_is_rejected_before_it_is_read(self) -> None:
        """超过"图片字节上限"换算出的请求体上限：连读都不读，直接 400。

        这里刻意把请求体撑大**而不是**把 base64 撑大（图片本身只有几十字节）：
        只有"先看 Content-Length"的那道闸会拦下它 —— `store()` 的逐字节检查
        要等 body 被读进来、base64 解完才可能触发，那样已经不是这条路径了。
        """
        settings = test_settings(
            self.tmp / "data-big", attachment_max_bytes=1024, request_timeout=1.0
        )
        self.server.settings = settings
        body = json.dumps(
            {
                "name": "x" * 40000,  # 纯占位：撑大的是**请求体**，不是图片
                "mime": "image/png",
                "data_base64": base64.b64encode(TINY_PNG).decode(),
            }
        ).encode("utf-8")
        self.assertGreater(len(body), _attachment_body_limit(1024))

        status, result = self._post_raw(
            "/api/attachments", body, {"Content-Type": "application/json"}
        )
        self.assertEqual(status, 400, result)
        self.assertIn("请求体", result["error"])
        self.assertIn("上限", result["error"])
        self.assertFalse(
            (settings.data_dir / "attachments").exists(), "被拒的请求不该落盘"
        )

    @needs_pillow
    def test_image_over_the_byte_limit_is_rejected_and_nothing_is_written(self) -> None:
        """闸门之下的那条上限仍然生效：`store()` 自己按真实字节数再判一次。

        （上一条证明的是"请求体太大就别读"；这一条证明"图片本身太大"照样拒绝，
        两处都得有 —— 前者是内存保护，后者才是契约 §7.1 的那条规则。）
        """
        settings = test_settings(
            self.tmp / "data-small", attachment_max_bytes=10, request_timeout=1.0
        )
        self.server.settings = settings
        status, result = self._upload(make_png(32, 32))
        self.assertEqual(status, 400, result)
        self.assertIn("上限", result["error"])
        self.assertFalse((settings.data_dir / "attachments").exists())

    def test_missing_pillow_reports_the_need_field_and_writes_nothing(self) -> None:
        """Pillow 缺失**明确失败**，不降级成"原样落盘"（契约 §6 末）。"""
        with mock.patch.object(attachments, "pillow_available", return_value=False):
            status, result = self._upload(TINY_PNG)
        self.assertEqual(status, 400)
        self.assertEqual(result["need"], "pillow")
        # 错误信息要给出可执行的下一步，而不是一句"缺少依赖"
        self.assertIn("setup", result["error"])
        self.assertFalse((self.settings.data_dir / "attachments").exists())


# --------------------------------------------------------------------------
# 取图（契约 §7.2）
# --------------------------------------------------------------------------


class ServeImageTests(WebCase):
    @needs_pillow
    def test_get_returns_the_bytes_with_hard_headers(self) -> None:
        data = make_png(50, 30)
        status, result = self._upload(data)
        self.assertEqual(status, 200, result)
        attachment = result["attachment"]

        status, body, headers = self._get_raw(attachment["url"])
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "image/png")
        # HTTP/1.1 的长连接靠 Content-Length 界定响应边界，少了它浏览器会把
        # 下一张图的响应拼到这一张后面
        self.assertEqual(headers.get("Content-Length"), str(len(body)))
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(
            headers.get("Cache-Control"), "private, max-age=31536000, immutable"
        )
        # 发出去的确实就是落盘的那一份（内容寻址 → 与 URL 一一对应）
        stored = (self.settings.data_dir / attachment["file"]).read_bytes()
        self.assertEqual(body, stored)
        self.assertEqual(len(body), attachment["stored_bytes"])

    def _url(self, session: str, note_id: str, ext: str) -> str:
        return "/api/attachments?" + urllib.parse.urlencode(
            {"session": session, "id": note_id, "ext": ext}
        )

    def test_bad_paths_are_rejected_with_400(self) -> None:
        """非法参数是输入问题（400）；两道闸（白名单 + 复查）都在 attachments 层。"""
        cases = {
            "session 穿越": ("../x", HEX_ID, "png"),
            "session 带分隔符": ("a/b", HEX_ID, "png"),
            "id 穿越": ("s1", "../../etc/passwd", "png"),
            "id 不是 hex": ("s1", "zzzzzzzzzzzzzzzz", "png"),
            "id 太短": ("s1", "a" * 15, "png"),
            "id 太长": ("s1", "a" * 65, "png"),
            "svg 永不支持": ("s1", HEX_ID, "svg"),
            "html 永不支持": ("s1", HEX_ID, "html"),
            "未知扩展名": ("s1", HEX_ID, "png.html"),
            "空 session": ("", HEX_ID, "png"),
        }
        for label, (session, note_id, ext) in cases.items():
            with self.subTest(case=label):
                status, body, _headers = self._get_raw(self._url(session, note_id, ext))
                self.assertEqual(status, 400, f"{label} 应当 400")
                payload = json.loads(body.decode("utf-8"))
                self.assertIn("error", payload)

    def test_missing_file_is_404_not_400(self) -> None:
        """参数合法但文件不在：404。与"参数不对"分开，因为要用户做的事不一样。"""
        status, body, _headers = self._get_raw(self._url("s-none", HEX_ID, "png"))
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(body.decode("utf-8")))

    def test_an_id_from_another_session_is_not_readable(self) -> None:
        """拿着 A 会话的哈希去 B 会话要图：路径按 (会话, 哈希, ext) 拼，读不到。"""
        victim = self.settings.data_dir / "attachments" / "s-a" / f"{HEX_ID}.png"
        victim.parent.mkdir(parents=True, exist_ok=True)
        victim.write_bytes(TINY_PNG)
        status, _body, _headers = self._get_raw(self._url("s-b", HEX_ID, "png"))
        self.assertEqual(status, 404)


# --------------------------------------------------------------------------
# 对话（契约 §7.3 / §5）
# --------------------------------------------------------------------------


class ChatAttachmentTests(WebCase):
    @needs_pillow
    def test_chat_with_an_attachment_reaches_the_model_and_keeps_the_log_clean(
        self,
    ) -> None:
        status, uploaded = self._upload(make_png(50, 30), name="cat.png")
        self.assertEqual(status, 200, uploaded)
        session_id = uploaded["session_id"]
        attachment = uploaded["attachment"]

        fake, status, body = self._chat_with_fake_provider(
            {
                "session_id": session_id,
                "text": "这是什么动物",
                "attachments": [
                    {"id": attachment["id"], "ext": "png", "name": "cat.png"}
                ],
            }
        )
        self.assertEqual(status, 200)
        self.assertIn("event: done", body)

        # 1) 模型真的收到了图片块（不是"服务端以为发了"）
        payloads = fake.calls[0]["payloads"]
        user = next(item for item in payloads if item["role"] == "user")
        self.assertIsInstance(user["content"], list)
        blocks = user["content"]
        self.assertEqual([block["type"] for block in blocks], ["text", "image_url"])
        self.assertEqual(blocks[0]["text"], "这是什么动物")
        self.assertTrue(blocks[1]["image_url"]["url"].startswith("data:image/png;base64,"))

        # 2) 会话 JSONL 的**每一行**都不许出现 base64 / data URL（契约 §5 硬规则）
        log_path = self.settings.sessions_dir / f"{session_id}.jsonl"
        lines = log_path.read_text(encoding="utf-8").splitlines()
        self.assertGreater(len(lines), 2)
        for number, line in enumerate(lines, 1):
            self.assertNotIn("base64,", line, f"第 {number} 行泄漏了 base64")
            self.assertNotIn("data:image/", line, f"第 {number} 行泄漏了 data URL")

        # 3) 事件里留下的是形状记录，前端刷新后也能看见这张图
        user_event = next(
            item for item in read_events(log_path) if item["kind"] == "user_message"
        )
        self.assertEqual(user_event["attachments"][0]["id"], attachment["id"])
        self.assertEqual(user_event["attachments"][0]["file"], attachment["file"])

        view = build_session_view(self.settings, session_id)
        first = view["messages"][0]
        self.assertEqual(first["role"], "user")
        self.assertEqual(first["attachments"][0]["url"], attachment["url"])
        self.assertEqual(first["attachments"][0]["name"], "cat.png")

    @needs_pillow
    def test_image_only_chat_is_accepted(self) -> None:
        """只贴图不说话 = 合法用法（老规则"内容不能为空"放宽为此）。"""
        status, uploaded = self._upload(make_png())
        self.assertEqual(status, 200, uploaded)
        attachment = uploaded["attachment"]
        session_id = uploaded["session_id"]

        fake, status, body = self._chat_with_fake_provider(
            {
                "session_id": session_id,
                "text": "",
                "attachments": [
                    {"id": attachment["id"], "ext": "png", "name": "cat.png"}
                ],
            }
        )
        self.assertEqual(status, 200, body[:200])
        self.assertIn("event: done", body)
        user = next(
            item for item in fake.calls[0]["payloads"] if item["role"] == "user"
        )
        # 没有文本就不发空文本块（契约 §4）
        self.assertEqual(
            [block["type"] for block in user["content"]], ["image_url"]
        )
        self.assertNotIn(
            "base64,",
            (self.settings.sessions_dir / f"{session_id}.jsonl").read_text(
                encoding="utf-8"
            ),
        )

    def test_text_and_attachments_may_not_both_be_empty(self) -> None:
        for payload in (
            {"text": "   "},
            {"text": "", "attachments": []},
            {"attachments": []},
        ):
            with self.subTest(payload=payload):
                status, result = self._post("/api/chat", payload)
                self.assertEqual(status, 400, result)
                self.assertIn("不能为空", result["error"])

    def test_attachments_must_be_a_list(self) -> None:
        status, result = self._post(
            "/api/chat", {"text": "你好", "attachments": "not-a-list"}
        )
        self.assertEqual(status, 400)
        self.assertIn("数组", result["error"])

    @needs_pillow
    def test_attachment_from_another_session_is_rejected(self) -> None:
        """**前端给的路径不可信**：id 必须在这个会话的目录里真的存在。

        上传到 s-owner，然后拿同一个 id 去 s-other 发消息 —— 服务器会在
        s-other 的目录里找，找不到就 400，并且一个字节都不写。
        """
        status, uploaded = self._upload(make_png(), session_id="s-owner")
        self.assertEqual(status, 200, uploaded)
        attachment = uploaded["attachment"]

        status, result = self._post(
            "/api/chat",
            {
                "session_id": "s-other",
                "text": "看图",
                "attachments": [
                    {"id": attachment["id"], "ext": "png", "name": "cat.png"}
                ],
            },
        )
        self.assertEqual(status, 400, result)
        self.assertIn("不存在", result["error"])
        self.assertFalse((self.settings.sessions_dir / "s-other.jsonl").exists())

    @needs_pillow
    def test_unknown_attachment_id_is_rejected(self) -> None:
        status, result = self._post(
            "/api/chat",
            {
                "session_id": "s-none",
                "text": "看图",
                "attachments": [{"id": HEX_ID, "ext": "png", "name": "cat.png"}],
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("不存在", result["error"])

    @needs_pillow
    def test_frontend_supplied_paths_and_sizes_are_ignored(self) -> None:
        """请求体里塞的 `file` / `width` 一概不算数 —— 磁盘是唯一真相（§7.3）。

        正面证明：附件目录之外放一个同名的"机密文件"，请求里把 `file` 指向它；
        最后发给模型的字节必须仍是附件目录里的那一份。
        """
        secret = self.settings.data_dir / "secret.png"
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_bytes(b"THIS-IS-NOT-THE-IMAGE")

        status, uploaded = self._upload(make_png(50, 30), session_id="s-safe")
        self.assertEqual(status, 200, uploaded)
        attachment = uploaded["attachment"]

        fake, status, body = self._chat_with_fake_provider(
            {
                "session_id": "s-safe",
                "text": "看图",
                "attachments": [
                    {
                        "id": attachment["id"],
                        "ext": "png",
                        "name": "改过的名字.png",
                        "file": "secret.png",
                        "mime": "image/png",
                        "width": 99999,
                        "height": 99999,
                    }
                ],
            }
        )
        self.assertEqual(status, 200, body[:200])
        user = next(
            item for item in fake.calls[0]["payloads"] if item["role"] == "user"
        )
        data_url = user["content"][1]["image_url"]["url"]
        encoded = data_url.split("base64,", 1)[1]
        self.assertEqual(
            base64.b64decode(encoded),
            (self.settings.data_dir / attachment["file"]).read_bytes(),
        )

        event = next(
            item
            for item in read_events(self.settings.sessions_dir / "s-safe.jsonl")
            if item["kind"] == "user_message"
        )
        record = event["attachments"][0]
        self.assertEqual(record["file"], attachment["file"], "落盘路径以磁盘为准")
        self.assertEqual((record["width"], record["height"]), (50, 30), "尺寸从磁盘重读")
        # name 是唯一允许由前端提供的字段（纯显示，绝不参与拼路径）
        self.assertEqual(record["name"], "改过的名字.png")


# --------------------------------------------------------------------------
# 删会话（契约 §2 末）
# --------------------------------------------------------------------------


class DeleteSessionAttachmentsTests(WebCase):
    def _session(self, name: str) -> Path:
        path = self.settings.sessions_dir / f"{name}.jsonl"
        log = EventLog(path, name)
        log.append("user_message", text="你好")
        log.close()
        return path

    def _attachment_file(self, session_id: str, name: str) -> Path:
        path = self.settings.data_dir / "attachments" / session_id / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(TINY_PNG)
        return path

    def test_deleting_a_session_removes_its_attachments_only(self) -> None:
        doomed = self._session("s-doomed")
        keep = self._session("s-keep")
        doomed_image = self._attachment_file("s-doomed", f"{HEX_ID}.png")
        keep_image = self._attachment_file("s-keep", f"{'b' * 16}.png")
        keep_before = keep_image.read_bytes()

        status, result = self._post("/api/sessions/s-doomed/delete", {})
        self.assertEqual(status, 200, result)
        self.assertTrue(result["ok"])
        self.assertEqual(result["attachments"], 1, "删掉了几张图要如实报出来")

        self.assertFalse(doomed.exists())
        self.assertFalse(doomed_image.exists())
        self.assertFalse(doomed_image.parent.exists(), "空掉的附件目录也要收掉")
        self.assertTrue(keep.exists(), "别的会话不能被动")
        self.assertTrue(keep_image.is_file(), "别的会话的附件一张都不能少")
        self.assertEqual(keep_image.read_bytes(), keep_before)

    def test_deleting_a_session_without_attachments_still_works(self) -> None:
        self._session("s-plain")
        status, result = self._post("/api/sessions/s-plain/delete", {})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["attachments"], 0)


class DreamLockOrderingTests(WebCase):
    """临界区必须在**响应离开服务端之前**结束。

    这里锁住的是一个真实存在过的 bug：`_handle_dream_format()` 先把 200 回出去、
    再在 `finally` 里释放锁。客户端读到 200 的那一刻，服务端线程未必已经跑到
    `finally` —— 于是**紧接着的下一个请求**（再格式化一次、或去保存档案）会拿到
    一句无中生有的「整理正在运行」。它只在请求**背靠背**时出现，中间插一句
    print 就没了，所以长期被当成"环境抖动"。
    """

    def test_format_releases_the_lock_before_answering(self) -> None:
        """连续 5 次格式化：**中间不许有任何 sleep/print**，一次 409 都不能有。

        这条用例的全部价值就在"中间什么都不做"：一旦在两次请求之间插进任何
        耗时动作，服务端线程就有时间跑完 `finally`，bug 会被藏起来（假绿）。
        """
        from harness.dream import dream_format_confirm

        payload = {"confirm": dream_format_confirm()}
        for attempt in range(5):
            status, result = self._post("/api/dream/format", payload)
            self.assertEqual(
                status,
                200,
                f"第 {attempt + 1} 次格式化拿到了 {status}：{result} —— "
                "锁在响应之后才释放，下一个请求就会看到假的「整理正在运行」",
            )
            self.assertIn("before", result)

    def test_dream_done_frame_is_sent_after_the_lock_is_released(self) -> None:
        """SSE 的终帧同理：不能一边握着整理锁、一边向客户端宣告"跑完了"。

        直接断言"发 dream_done 的那一刻锁是否还握着" —— 比"发完立刻再请求一次"
        稳定得多（后者依赖两个线程的调度，天生会飘）。这是同一类 bug 的结构性检查。
        """
        observed: list[bool] = []
        original = HarnessHandler._safe_sse

        def spy(handler, event, payload):  # type: ignore[no-untyped-def]
            if event == "dream_done":
                observed.append(handler.server.dream_lock.locked())
            return original(handler, event, payload)

        with mock.patch.object(HarnessHandler, "_safe_sse", spy):
            status, body = self._post_sse("/api/dream", {})

        self.assertEqual(status, 200)
        self.assertIn("event: dream_done", body)
        self.assertEqual(observed, [False], "dream_done 是在还握着整理锁的时候发出去的")

    def test_chat_done_frame_is_sent_after_the_session_lock_is_released(self) -> None:
        """对话那条路也一样：`done` 一发出，界面就会允许"再发一条 / 删这个会话"。

        所以"发 done"必须发生在释放会话锁之后 —— 否则用户点完一轮、立刻再发，
        会看到一句假的"这个会话正在生成中"。
        """
        observed: list[bool] = []
        original = HarnessHandler._safe_sse

        def spy(handler, event, payload):  # type: ignore[no-untyped-def]
            if event == "done":
                lock = handler.server.session_lock(str(payload["session_id"]))
                observed.append(lock.locked())
            return original(handler, event, payload)

        fake = FakeProvider([text_reply("好的")])
        resolved = replace(resolve_provider(self.settings), provider=fake)
        with mock.patch.object(web, "resolve_provider", lambda *a, **k: resolved):
            with mock.patch.object(HarnessHandler, "_safe_sse", spy):
                status, body = self._post_sse("/api/chat", {"text": "你好"})

        self.assertEqual(status, 200)
        self.assertIn("event: done", body)
        self.assertEqual(observed, [False], "done 是在还握着会话锁的时候发出去的")


# --------------------------------------------------------------------------
# 重置预览（契约 §10 末）
# --------------------------------------------------------------------------


class ResetPreviewTests(WebCase):
    def test_preview_reports_the_attachment_count(self) -> None:
        """不能出现"预览说 0 个文件、其实删掉了 20 张图"。"""
        before = preview_reset(self.settings)
        self.assertEqual((before["attachment_files"], before["attachment_bytes"]), (0, 0))

        for index in range(3):
            path = (
                self.settings.data_dir
                / "attachments"
                / "s1"
                / f"{index:016x}.png"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(TINY_PNG)

        preview = preview_reset(self.settings)
        self.assertEqual(preview["attachment_files"], 3)
        self.assertGreater(preview["attachment_bytes"], 0)
        self.assertGreaterEqual(preview["bytes"], preview["attachment_bytes"])
        # 预览是只读的：数一遍不许创建任何东西
        self.assertTrue((self.settings.data_dir / "attachments" / "s1").is_dir())


# --------------------------------------------------------------------------
# 整理输入（契约 §9）
# --------------------------------------------------------------------------


class DreamRecordTests(unittest.TestCase):
    def test_an_image_only_message_no_longer_disappears(self) -> None:
        line = render_event(
            {"kind": "user_message", "text": "", "attachments": [{"id": HEX_ID}]}
        )
        self.assertEqual(line, "用户：[图片 1 张]")

    def test_text_only_messages_render_exactly_as_before(self) -> None:
        self.assertEqual(render_event({"kind": "user_message", "text": "你好"}), "用户：你好")
        self.assertIsNone(render_event({"kind": "user_message", "text": ""}))

    def test_text_plus_images_keeps_the_text_and_marks_the_count(self) -> None:
        line = render_event(
            {
                "kind": "user_message",
                "text": "这是什么动物",
                "attachments": [{"id": HEX_ID, "name": "cat.png"}, {"id": "b" * 16}],
            }
        )
        self.assertIn("这是什么动物", line)
        self.assertIn("2 张图片", line)
        # 文件名不进整理输入：它对"发生了什么"没有信息量，只会变成噪声
        self.assertNotIn("cat.png", line)

    def test_broken_attachment_field_does_not_break_the_render(self) -> None:
        for value in ("nope", 3, None):
            with self.subTest(value=value):
                self.assertEqual(
                    render_event({"kind": "user_message", "text": "你好", "attachments": value}),
                    "用户：你好",
                )


if __name__ == "__main__":
    unittest.main()
