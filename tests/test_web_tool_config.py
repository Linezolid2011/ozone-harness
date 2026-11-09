"""工具配置的 Web 层测试（ADR-114）：GET/POST /api/tools/config、
旧 /tools 端点的配置化、chat 按配置装配工具定义、删会话连带删配置。

服务是真 HarnessServer，provider 是脚本化的 FakeProvider：不联网、不花钱。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest import mock

from harness import web
from harness.events import EventLog, read_events
from harness.runtime import resolve_provider
from harness.tool_config import CHAT_TOOLS, ToolConfigStore
from harness.web import HarnessHandler, HarnessServer, build_session_view

from .helpers import FakeProvider, seed_provider, test_settings, text_reply

NO_WEB = [n for n in CHAT_TOOLS if n not in ("web_search", "web_open_url")]


class ToolConfigWebCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.settings = test_settings(self.tmp / "data", request_timeout=1.0)
        seed_provider(self.settings, base_url="http://127.0.0.1:1")
        self.server = HarnessServer(("127.0.0.1", 0), HarnessHandler)
        self.server.settings = self.settings
        self.server.verbose = False
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.store = ToolConfigStore(self.settings.data_dir)

    # -- HTTP 小工具 ------------------------------------------------------

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
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")

    def _get(self, path: str) -> tuple[int, dict]:
        request = urllib.request.Request(self.base + path, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")

    def _chat(self, payload: dict):
        fake = FakeProvider([text_reply("好的")])
        real = resolve_provider

        def resolve_with_fake(settings, **kwargs):
            return replace(real(settings, **kwargs), provider=fake)

        with mock.patch.object(web, "resolve_provider", resolve_with_fake):
            status, body = self._sse("/api/chat", payload)
        return fake, status, body

    def _sse(self, path: str, payload: dict) -> tuple[int, str]:
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

    def _make_log(self, name: str, *records: tuple[str, dict]) -> None:
        """写事件后**立即关句柄**：Windows 上开着句柄的文件删不掉，
        而这个测试类接下来要删/分叉这些会话。"""
        log = EventLog(self.settings.sessions_dir / f"{name}.jsonl", name)
        try:
            for kind, data in records:
                log.append(kind, **data)
        finally:
            log.close()


class GetConfigTests(ToolConfigWebCase):
    def test_get_is_read_only_and_lists_everything(self) -> None:
        """初始 GET：default/session 都是 null（不种文件），all 是全工具名单。"""
        status, body = self._get("/api/tools/config")
        self.assertEqual(status, 200)
        self.assertIsNone(body["default"])
        self.assertIsNone(body["session"])
        names = [item["name"] for item in body["all"]]
        self.assertEqual(names, sorted(CHAT_TOOLS))
        for item in body["all"]:
            self.assertTrue(item["label"])
            self.assertTrue(item["hint"])
        # 只读：一个文件都不许出现
        self.assertFalse((self.settings.data_dir / "tool_config.json").exists())
        self.assertFalse((self.settings.data_dir / "tool_config").exists())

    def test_get_groups_partition_all_tools(self) -> None:
        """ADR-115：groups 按 tool_groups.json 返回，组带本地化显示名，
        组内工具恰好划分全工具名单（不重复、不遗漏）。"""
        from harness.tool_groups import load_groups

        status, body = self._get("/api/tools/config")
        self.assertEqual(status, 200)
        groups = body["groups"]
        expected = [(g.id, list(g.tools)) for g in load_groups(quiet=True)]
        actual = [
            (group["id"], [item["name"] for item in group["tools"]])
            for group in groups
        ]
        self.assertEqual(actual, expected)
        for group in groups:
            self.assertTrue(group["label"], f"组 {group['id']} 缺显示名")
            for item in group["tools"]:
                self.assertTrue(item["label"])
                self.assertTrue(item["hint"])

    def test_get_with_session_is_read_only(self) -> None:
        status, body = self._get("/api/tools/config?session=ghost-session")
        self.assertEqual(status, 200)
        self.assertIsNone(body["session"])
        self.assertFalse((self.settings.data_dir / "tool_config").exists())

    def test_get_with_bad_session_id_is_400(self) -> None:
        status, _body = self._get("/api/tools/config?session=../escape")
        self.assertEqual(status, 400)

    def test_get_reflects_saved_values(self) -> None:
        self._post("/api/tools/config", {"tools": ["mem_create", "time"]})
        self._post(
            "/api/tools/config", {"session_id": "s1", "tools": ["time"]}
        )
        status, body = self._get("/api/tools/config?session=s1")
        self.assertEqual(status, 200)
        self.assertEqual(body["default"], ["mem_create", "time"])
        self.assertEqual(body["session"], ["time"])


class SaveConfigTests(ToolConfigWebCase):
    def test_save_default(self) -> None:
        status, body = self._post(
            "/api/tools/config", {"tools": ["web_search", "mem_create", "time"]}
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["tools"], ["mem_create", "time", "web_search"])
        self.assertEqual(self.store.read_default(), body["tools"])

    def test_save_default_rejects_unknown_tool(self) -> None:
        status, body = self._post("/api/tools/config", {"tools": ["fs_rm_rf"]})
        self.assertEqual(status, 400)
        self.assertIn("fs_rm_rf", body["error"])
        self.assertIsNone(self.store.read_default())

    def test_save_default_rejects_malformed(self) -> None:
        for bad in ({"tools": "web_search"}, {"tools": ["time", "time"]}, {}):
            status, body = self._post("/api/tools/config", bad)
            self.assertEqual(status, 400, bad)
            self.assertIn("error", body)

    def test_save_session_writes_event_for_audit(self) -> None:
        self._make_log("s1", ("user_message", {"text": "你好"}))
        status, body = self._post(
            "/api/tools/config", {"session_id": "s1", "tools": ["mem_create"]}
        )
        self.assertEqual(status, 200, body)
        self.assertFalse(body["web_search"])
        self.assertTrue(body["multimodal"])  # 缺键 = 开，同写两个老键
        record = next(
            item
            for item in read_events(self.settings.sessions_dir / "s1.jsonl")
            if item["kind"] == "session_tools"
        )
        self.assertEqual(record["tools"], ["mem_create"])
        self.assertFalse(record["web_search"])
        # 会话视图读得出名单
        view = build_session_view(self.settings, "s1")
        self.assertEqual(view["tools"]["enabled"], ["mem_create"])
        self.assertFalse(view["tools"]["web_search"])

    def test_save_session_bad_id_is_400_and_no_write(self) -> None:
        status, _body = self._post(
            "/api/tools/config", {"session_id": "../escape", "tools": ["time"]}
        )
        self.assertEqual(status, 400)
        self.assertFalse((self.settings.data_dir / "tool_config").exists())

    def test_save_session_derives_web_search_key(self) -> None:
        self._make_log("s1", ("user_message", {"text": "你好"}))
        self._post(
            "/api/tools/config",
            {"session_id": "s1", "tools": ["web_search", "web_open_url"]},
        )
        view = build_session_view(self.settings, "s1")
        self.assertTrue(view["tools"]["web_search"])


class LegacyToolsEndpointTests(ToolConfigWebCase):
    """旧 `/api/sessions/<id>/tools`：multimodal 语义不动，web_search 改为写配置。"""

    def test_web_search_toggle_edits_config(self) -> None:
        self._make_log("s1", ("user_message", {"text": "你好"}))
        status, body = self._post("/api/sessions/s1/tools", {"web_search": True})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["web_search"])
        enabled = self.store.read_session("s1") or []
        # 开关打开 = 两个联网工具一起进配置（ADR-114 第 5 条）
        self.assertIn("web_search", enabled)
        self.assertIn("web_open_url", enabled)

        self._post("/api/sessions/s1/tools", {"web_search": False})
        enabled = self.store.read_session("s1") or []
        self.assertNotIn("web_search", enabled)
        self.assertNotIn("web_open_url", enabled)

    def test_multimodal_alone_leaves_config_at_default(self) -> None:
        """只切多模态：配置原样出生（默认 = 全部启用），事件照常写。"""
        self._make_log("s1", ("user_message", {"text": "你好"}))
        status, body = self._post("/api/sessions/s1/tools", {"multimodal": False})
        self.assertEqual(status, 200, body)
        self.assertFalse(body["multimodal"])
        self.assertTrue(body["web_search"])  # 默认配置里开着
        self.assertEqual(self.store.read_session("s1"), sorted(CHAT_TOOLS))


class ChatWiringTests(ToolConfigWebCase):
    def test_chat_registers_only_configured_tools(self) -> None:
        """发出的工具定义与会话配置一致（FakeProvider 断言 tools 数组）。"""
        self.store.save_session("s1", NO_WEB)
        fake, status, _body = self._chat({"session_id": "s1", "text": "你好"})
        self.assertEqual(status, 200)
        names = [item["function"]["name"] for item in fake.calls[0]["tools"]]
        self.assertNotIn("web_search", names)
        self.assertNotIn("web_open_url", names)
        self.assertIn("mem_create", names)

    def test_chat_births_session_config_before_first_call(self) -> None:
        """会话出生在这一刻完成：配置复制自默认，且在第一发请求之前。"""
        self.store.save_default(["mem_create", "time"])
        fake, status, _body = self._chat({"session_id": "s-born", "text": "你好"})
        self.assertEqual(status, 200)
        self.assertEqual(self.store.read_session("s-born"), ["mem_create", "time"])
        names = [item["function"]["name"] for item in fake.calls[0]["tools"]]
        self.assertEqual(names, ["mem_create", "time"])


class LifecycleTests(ToolConfigWebCase):
    def test_delete_session_removes_config(self) -> None:
        self._make_log("s1", ("user_message", {"text": "你好"}))
        self.store.save_session("s1", ["mem_create"])
        status, _body = self._post("/api/sessions/s1/delete", {})
        self.assertEqual(status, 200)
        self.assertIsNone(self.store.read_session("s1"))

    def test_fork_copies_config(self) -> None:
        self._make_log("s1", ("user_message", {"text": "你好"}))
        self.store.save_session("s1", ["mem_create"])
        status, body = self._post("/api/sessions/s1/fork", {"seq": 1})
        self.assertEqual(status, 200, body)
        new_sid = body["session_id"]
        self.assertEqual(self.store.read_session(new_sid), ["mem_create"])
        # 源会话的配置不受分叉影响
        self.assertEqual(self.store.read_session("s1"), ["mem_create"])


if __name__ == "__main__":
    unittest.main()
