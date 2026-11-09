"""Web 端测试（M5）。真实起 HTTP 服务，但**不联网**、不花钱。

联网的那条路径用 "base_url 指向 127.0.0.1:1" 来触发连接失败 ——
这样能完整走通 SSE 管道（session → notice → turn_end → done），
验证的是我们自己代码的帧格式与错误传播，而不是网络。
"""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock

from harness import i18n
from harness.events import EventLog
from harness.llm_types import Message, ToolCall
from harness.memory import MemoryStore, open_memory
from harness.tools.registry import ToolResult
from harness.web import (
    HarnessHandler,
    HarnessServer,
    build_memory_view,
    build_session_view,
    forward_event,
    port_is_taken,
    serve,
)

from .helpers import FakeProvider, seed_provider, test_settings


class ForwardEventTests(unittest.TestCase):
    """emit 事件 → SSE 帧的映射：纯函数，最该被钉死的一层。"""

    def test_text_and_reasoning(self) -> None:
        self.assertEqual(forward_event("text", {"text": "hi"}), ("text", {"text": "hi"}))
        self.assertEqual(
            forward_event("reasoning", {"text": "想"}), ("reasoning", {"text": "想"})
        )

    def test_prompt_events_are_dropped(self) -> None:
        """完整提示词是调试用的，不该每轮推给浏览器。"""
        self.assertIsNone(forward_event("prompt", {"messages": [], "estimated_tokens": 1}))
        self.assertIsNone(forward_event("unknown_kind", {}))

    def test_tool_events(self) -> None:
        call = ToolCall(id="c1", name="mem_create", arguments='{"content":"x"}')
        result = ToolResult(ok=True, payload={"id": 7}, display="记住 #7")
        self.assertEqual(
            forward_event("tool_call", {"call": call}),
            ("tool_call", {"name": "mem_create", "arguments": '{"content":"x"}'}),
        )
        name, payload = forward_event("tool_result", {"call": call, "result": result})
        self.assertEqual(name, "tool_result")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"], {"id": 7})

    def test_turn_end_carries_the_user_seq(self) -> None:
        """前端「分叉」按钮靠 turn_end 帧里的 user_seq 定位，实时流那一侧必须有它。"""
        from harness.loop import TurnOutcome

        name, payload = forward_event(
            "turn_end",
            {"outcome": TurnOutcome(text="hi", steps=1, user_seq=42, context_tokens=123)},
        )
        self.assertEqual(name, "turn_end")
        self.assertEqual(payload["user_seq"], 42)
        self.assertEqual(payload["context_tokens"], 123)
        # 老构造方式（没有 user_seq）不能炸，给 None 就行
        name, payload = forward_event("turn_end", {"outcome": TurnOutcome(text="hi")})
        self.assertIsNone(payload["user_seq"])
        self.assertEqual(payload["context_tokens"], 0)


class ViewBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = test_settings(Path(self._tmp.name))

    def test_session_view_replays_the_event_log(self) -> None:
        log = EventLog(self.settings.sessions_dir / "s1.jsonl", "s1")
        log.append("user_message", text="记住我喜欢美式")
        log.append(
            "assistant_message",
            step=1,
            finish_reason="tool_calls",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            message=Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall("c1", "mem_create", "{}")],
                reasoning="想想",
            ).to_record(),
        )
        log.append(
            "tool_call",
            step=1,
            tool_call_id="c1",
            name="mem_create",
            arguments="{}",
            ok=True,
            display="记住 #1",
            result={"id": 1},
            result_content="{}",
        )
        log.append("notice", level="warn", text="注意")
        log.append("turn_end", steps=1, stop_reason="completed", elapsed=1.2, usage={})
        log.close()

        view = build_session_view(self.settings, "s1")
        roles = [message["role"] for message in view["messages"]]
        self.assertEqual(roles, ["user", "assistant", "tool", "notice", "meta"])
        self.assertEqual(view["messages"][1]["reasoning"], "想想")
        self.assertEqual(view["messages"][2]["display"], "记住 #1")
        self.assertEqual(view["usage"]["total_tokens"], 15)
        self.assertEqual(view["stats"]["turns"], 1)
        self.assertEqual(view["stats"]["elapsed_seconds"], 1.2)
        self.assertEqual(view["stats"]["usage"]["total_tokens"], 15)
        self.assertEqual(view["stats"]["context_tokens"], 10)

    def test_session_stats_use_the_last_step_context_and_include_summary_usage(self) -> None:
        """统计条的上下文不是多步输入之和，摘要调用的真实开销也不能漏。"""
        log = EventLog(self.settings.sessions_dir / "s-stats.jsonl", "s-stats")
        for step, prompt in ((1, 100), (2, 180)):
            log.append(
                "assistant_message",
                step=step,
                finish_reason="stop",
                usage={
                    "prompt_tokens": prompt,
                    "completion_tokens": 20,
                    "total_tokens": prompt + 20,
                    "cached_tokens": prompt // 2,
                },
                message=Message(role="assistant", content="答").to_record(),
            )
        log.append(
            "session_summary",
            summary="摘要",
            covered_messages=2,
            usage={"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50},
        )
        log.append(
            "turn_end",
            steps=2,
            stop_reason="completed",
            elapsed=2.5,
            usage={},
        )
        log.close()

        stats = build_session_view(self.settings, "s-stats")["stats"]
        self.assertEqual(stats["turns"], 1)
        self.assertEqual(stats["elapsed_seconds"], 2.5)
        self.assertEqual(stats["context_tokens"], 180)
        self.assertEqual(stats["usage"]["prompt_tokens"], 320)
        self.assertEqual(stats["usage"]["completion_tokens"], 50)
        self.assertEqual(stats["usage"]["total_tokens"], 370)
        self.assertEqual(stats["usage"]["cached_tokens"], 140)

    def test_meta_line_carries_the_turn_seq_for_the_fork_button(self) -> None:
        """每轮结尾那行统计要带这一轮 user_message 的 seq —— 「分叉」按钮靠它定位。"""
        log = EventLog(self.settings.sessions_dir / "s-fork.jsonl", "s-fork")
        first = log.append("user_message", text="第一问")
        log.append(
            "assistant_message", step=1, finish_reason="stop",
            usage={"total_tokens": 5},
            message=Message(role="assistant", content="答一").to_record(),
        )
        log.append("turn_end", steps=1, stop_reason="completed", elapsed=1.0, usage={})
        second = log.append("user_message", text="第二问")
        log.append(
            "assistant_message", step=1, finish_reason="stop",
            usage={"total_tokens": 5},
            message=Message(role="assistant", content="答二").to_record(),
        )
        log.append("turn_end", steps=1, stop_reason="completed", elapsed=1.0, usage={})
        log.close()

        view = build_session_view(self.settings, "s-fork")
        metas = [m for m in view["messages"] if m["role"] == "meta"]
        self.assertEqual([m["turn_seq"] for m in metas], [first["seq"], second["seq"]])

    def test_missing_session_reports_error(self) -> None:
        self.assertIn("error", build_session_view(self.settings, "nope"))

    def test_memory_view_shape(self) -> None:
        memory = open_memory(self.settings)
        try:
            memory.create("小林喜欢美式咖啡")
        finally:
            memory.close()

        view = build_memory_view(self.settings)
        self.assertEqual(view["stats"]["active"], 1)
        self.assertEqual(view["items"][0]["content"], "小林喜欢美式咖啡")
        # 别名机制已删（ADR-058），连字段也在 M7 迁移里删了（ADR-059）：
        # 前端不该再拿到这三个已死的键
        for dead in ("aliases", "sources", "dreamed_at"):
            self.assertNotIn(dead, view["items"][0])
        self.assertNotIn("dream_runs", view["stats"])
        # 上限状态与"有没有条目没进上下文"（ADR-057 / ADR-058 之后新增的两块）
        self.assertEqual(
            view["limit"],
            {
                "max_items": 50,
                "active_count": 1,
                "min_allowed": 0,
                "max_allowed": 100,
                "lowest_now": 1,
            },
        )
        self.assertEqual(view["injection"], {"omitted": 0, "token_budget": 6000})


class HttpServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # base_url 指向一个连不通的端口：走完 SSE 管道但绝不联网
        self.settings = test_settings(
            Path(self._tmp.name),
            request_timeout=1.0,
        )
        # 接口是"用户自己加的"，不是从 .env 种出来的
        seed_provider(self.settings, base_url="http://127.0.0.1:1")
        self.server = HarnessServer(("127.0.0.1", 0), HarnessHandler)
        self.server.settings = self.settings
        self.server.verbose = False
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    # -- 基础 --------------------------------------------------------

    def _get(
        self, path: str, headers: dict[str, str] | None = None
    ) -> tuple[int, bytes, str]:
        request = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                return resp.status, resp.read(), resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers.get("Content-Type", "")

    def _post(self, path: str, payload: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_serves_the_page_and_assets(self) -> None:
        status, body, content_type = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn("O₃ᶻᵒⁿᵉ", body.decode("utf-8"))

        for asset, expected in (("/style.css", "text/css"), ("/app.js", "javascript")):
            status, body, content_type = self._get(asset)
            self.assertEqual(status, 200, asset)
            self.assertIn(expected, content_type)
            self.assertGreater(len(body), 100)

    def test_blocks_path_traversal(self) -> None:
        """静态文件不能跑出 web/ 目录 —— 否则 .env 和 API key 就泄了。"""
        for path in ("/../.env", "/../harness/config.py", "/..%2f.env"):
            status, _body, _ct = self._get(path)
            self.assertIn(status, (400, 404), f"{path} 居然返回了 {status}")

    def test_config_and_sessions_and_usage(self) -> None:
        status, body, _ = self._get("/api/config")
        self.assertEqual(status, 200)
        config = json.loads(body)
        # model 现在指的是"当前选中的那个接口的模型"，不再是全局唯一的一个
        self.assertEqual(config["model"], "deepseek-flash")
        self.assertTrue(config["has_key"])
        self.assertEqual(config["provider_count"], 1)
        # 版本号来自 `docs/version.txt`（ADR-096）—— 不是代码里写死的常量
        from harness import version

        self.assertEqual(config["version"], version())
        self.assertTrue(config["version"], "版本号不能是空串")
        # 单轮最大输出（ADR-101）：默认是 .env 种子值（测试 Settings 里是 256）
        self.assertEqual(config["max_output_tokens"], self.settings.max_output_tokens)

        status, body, _ = self._get("/api/sessions")
        self.assertEqual(json.loads(body)["sessions"], [])

        status, body, _ = self._get("/api/usage")
        self.assertEqual(json.loads(body)["total"]["total_tokens"], 0)

    def test_unknown_api_returns_404(self) -> None:
        status, _body, _ct = self._get("/api/nope")
        self.assertEqual(status, 404)

    # -- 记忆接口 ----------------------------------------------------

    def test_memory_endpoints(self) -> None:
        from harness.memory import MemoryStore

        store = MemoryStore(instruction_path=self.settings.memory_instruction_path, cursor_db_path=self.settings.memory_db_path)
        memory_id = store.create("小林喜欢美式咖啡")["id"]
        store.close()

        status, body, _ = self._get("/api/memories")
        payload = json.loads(body)
        self.assertEqual(payload["stats"], {"active": 1})
        self.assertEqual(set(payload["items"][0]), {"id", "content"})

        status, result = self._post(
            "/api/memories/update", {"id": memory_id, "content": "小林更喜欢拿铁"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])

        status, result = self._post("/api/memories/delete", {"id": memory_id})
        self.assertTrue(result["ok"])
        status, body, _ = self._get("/api/memories")
        self.assertEqual(json.loads(body)["stats"]["active"], 0)

    def test_memory_limit_endpoint(self) -> None:
        """改记忆条数上限（ADR-057）：成功 / 超范围 / 低于当前条数都要有确定回答。"""
        memory = open_memory(self.settings)
        try:
            for index in range(3):
                self.assertTrue(memory.create(f"事实编号 {index}")["ok"])
        finally:
            memory.close()

        # 改之前先看一眼：GET 是只读的，**不能**把 memory.json 种出来
        self._get("/api/memories")
        self.assertFalse(
            (self.settings.data_dir / "memory.json").is_file(),
            "只是看一眼就把配置文件写出来了？",
        )

        # 成功：写入 60，并且立刻在 GET 里生效
        status, result = self._post("/api/memories/limit", {"max_items": 60})
        self.assertEqual(status, 200, result)
        self.assertTrue(result["ok"])
        self.assertEqual(result["limit"]["max_items"], 60)

        status, body, _ = self._get("/api/memories")
        state = json.loads(body)["limit"]
        self.assertEqual(state["max_items"], 60, "上限改了要立刻生效")
        self.assertEqual(state["active_count"], 3)
        self.assertEqual(state["lowest_now"], 3, "界面要知道最低能设到几")
        self.assertTrue((self.settings.data_dir / "memory.json").is_file())

        # 低于当前条数：400，且错误信息要说清"现在有几条"
        status, result = self._post("/api/memories/limit", {"max_items": 2})
        self.assertEqual(status, 400)
        self.assertIn("上限太低", result["error"])
        self.assertIn("3 条", result["error"])

        # 等于当前条数是允许的（边界值）
        status, result = self._post("/api/memories/limit", {"max_items": 3})
        self.assertEqual(status, 200, result)

        # 超范围与非法输入
        for bad in (101, -1, "abc", None, True):
            with self.subTest(bad=bad):
                status, result = self._post("/api/memories/limit", {"max_items": bad})
                self.assertEqual(status, 400, f"{bad!r} 应当被拒绝")
                self.assertIn("error", result)

    def test_every_route_answers_its_own_request(self) -> None:
        """**一个请求只能产生一个响应** —— 这条是连接复用上的真 bug 换来的。

        `protocol_version = "HTTP/1.1"`，所以客户端的连接会被复用。某个分支
        少写一个 `return`，就会在**同一条连接上多写出一个响应**，而客户端会把那个
        多余的响应当成下一个请求的回答：表现是"上一步明明成功了、下一步报了个
        莫名其妙的 404"，而且换一条连接就不复现 —— 极难查。
        （实测复现过：`POST /api/memories/limit` 之后紧跟的 `GET /api/config`
        收到的是 `404 {"error": "未知接口 /api/memories/limit"}`。）

        判据刻意选成**"同一条连接上的下一条请求能不能拿到它自己的回答"**，
        而不是"socket 里还有没有多余字节"：`http.client` 自己的读缓冲会吃掉
        一部分多余字节，那样写抓不到（第一版就是那么写的）。
        """
        # 造一个会话，好让 `/api/sessions/<id>/*` 那几条分支也进网
        log = EventLog(self.settings.sessions_dir / "s-route.jsonl", "s-route")
        try:
            log.append("user_message", text="你好")
        finally:
            log.close()

        cases: list[tuple[str, str, dict[str, Any] | None]] = [
            ("GET", "/api/config", None),
            ("GET", "/api/memories", None),
            ("GET", "/api/sessions/s-route", None),
            ("GET", "/api/nope", None),
            # ↓ 就是漏掉 return 的那一条（连接复用时会污染下一个请求）
            ("POST", "/api/memories/limit", {"max_items": 60}),
            ("POST", "/api/memories/limit", {"max_items": 101}),
            ("POST", "/api/memories/update", {"id": "abc"}),
            ("POST", "/api/sessions/s-route/settings", {"model": "m"}),
            ("POST", "/api/sessions/s-route/tools", {"web_search": True}),
            ("POST", "/api/sessions/s-route/title", {"title": "标题"}),
            ("POST", "/api/tools/save", {}),
            ("POST", "/api/tools/test", {"query": ""}),
            ("POST", "/api/providers/default", {}),
            ("POST", "/api/providers/save", {
                "id": "extra", "label": "Extra", "base_url": "http://127.0.0.1:1",
            }),
            ("POST", "/api/prompts/update", {
                "language": "zh", "id": "TAIL_NOW", "text": "x {stamp}",
            }),
            ("POST", "/api/prompts/revert", {"language": "zh"}),
            ("POST", "/api/settings/language", {"language": "zh"}),
            ("POST", "/api/vault/file", {"path": "../x.md", "body": "x"}),
            ("POST", "/api/vault/file/delete", {"path": "../x.md"}),
            ("POST", "/api/dreams/dream-x/undo", {}),
            ("POST", "/api/nope", {}),
            # 破坏性的两条放最后：它们真的会动这个临时数据目录
            ("POST", "/api/sessions/s-route/delete", {}),
        ]
        for method, path, payload in cases:
            with self.subTest(method=method, path=path):
                self._assert_next_request_gets_its_own_answer(method, path, payload)

    def _assert_next_request_gets_its_own_answer(
        self, method: str, path: str, payload: dict[str, Any] | None
    ) -> None:
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=10
        )
        self.addCleanup(conn.close)
        body = None
        headers: dict[str, str] = {}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body, headers)
        conn.getresponse().read()  # 第一条只看"它回答了"，响应体不关心

        conn.request("GET", "/api/config")
        second = conn.getresponse()
        raw = second.read()
        self.assertEqual(
            second.status,
            200,
            f"{method} {path} 之后，同一条连接上的 GET /api/config 拿到了 "
            f"{second.status} {raw[:200].decode('utf-8', 'replace')!r} —— "
            "上一条请求是不是多写了一个响应？",
        )
        self.assertIn(
            "data_dir",
            json.loads(raw.decode("utf-8")),
            f"{method} {path} 之后的回答不是 /api/config 自己的",
        )

    def test_memory_endpoint_rejects_bad_input(self) -> None:
        status, result = self._post("/api/memories/update", {"id": "abc"})
        self.assertEqual(status, 400)
        self.assertIn("整数", result["error"])

    def test_memory_endpoint_rejects_duplicate(self) -> None:
        from harness.memory import MemoryStore

        store = MemoryStore(instruction_path=self.settings.memory_instruction_path, cursor_db_path=self.settings.memory_db_path)
        first = store.create("小林喜欢美式咖啡")["id"]
        second = store.create("小林喜欢手冲咖啡")["id"]
        store.close()

        status, result = self._post(
            "/api/memories/update", {"id": second, "content": "小林喜欢美式咖啡"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(result["existing_id"], first)

    # -- 背景档案与整理运行（M7）--------------------------------------

    def _seed_dream_run(self, *, mode: str = "apply") -> str:
        """造一次真实的整理运行：vault + 运行记录（用真写入器，不手搓 JSON）。

        **不再造快照** —— ADR-065 取消了回滚，`data/dreams/` 下只有记录与轨迹。
        """
        from harness.dream import DreamRunStore
        from harness.vault import VaultStore

        vault = VaultStore(
            self.settings.vault_dir,
            readme_token_budget=self.settings.vault_readme_token_budget,
        )
        vault.write_file("sections/口味.md", "手冲，不加糖。")
        store = DreamRunStore(self.settings.dreams_dir)
        run_id = "dream-20261012-101500-ab12"
        vault.write_file("entities/小林.md", "整理过程中新建的实体")
        store.save(
            {
                "id": run_id,
                "started_at": "2026-10-12T10:15:00+08:00",
                "finished_at": "2026-10-12T10:16:00+08:00",
                "mode": mode,
                "status": "completed",
                "pending": {"sessions": 1, "events": 12, "detail": []},
                "advanced": {"s-1": 12},
                "diff": {
                    "added": [{"path": "entities/小林.md", "title": "小林", "chars": 9, "summary": "x"}],
                    "changed": [],
                    "removed": [],
                },
                "check": {
                    "exists": True,
                    "files": 1,
                    "has_readme": False,
                    "readme_tokens": 0,
                    "readme_budget": 1200,
                    "readme_truncated": False,
                    "problems": ["有档案文件但没有 README：模型将看不到任何索引"],
                },
                "summary": "新建了小林这份档案。",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                "stop_reason": "completed",
            }
        )
        return run_id

    def test_vault_endpoint_reports_files_budget_and_pending(self) -> None:
        log = EventLog(self.settings.sessions_dir / "s-1.jsonl", "s-1")
        log.append("user_message", text="你好")
        log.close()
        self._seed_dream_run()

        status, body, _ = self._get("/api/vault")
        self.assertEqual(status, 200)
        view = json.loads(body)
        self.assertTrue(view["check"]["exists"])
        self.assertEqual(view["check"]["files"], 2)
        self.assertIn("有档案文件但没有 README", "\n".join(view["check"]["problems"]))
        # 待整理：那个会话还没整理过，所以应当被算进来
        self.assertEqual(view["pending"]["sessions"], 1)
        paths = {item["path"] for item in view["files"]}
        self.assertEqual(paths, {"sections/口味.md", "entities/小林.md"})
        self.assertEqual(view["check"]["readme_budget"], self.settings.vault_readme_token_budget)

    def test_prompts_endpoint_needs_no_provider_and_writes_nothing(self) -> None:
        """「设置 → 提示词」：**一个接口都没配**也要能打开，而且不许写盘。

        这一页是"我们到底跟模型说了什么"的自查页，所以它必须在你还什么都没配的
        时候就能看 —— 如果它先要求配置接口，那它对新用户等于不存在。
        """
        empty = test_settings(Path(self._tmp.name) / "prompts-fresh")
        self.server.settings = empty

        status, body, ctype = self._get("/api/prompts")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        view = json.loads(body)
        self.assertEqual(view["summary"]["prompts"], 22)
        self.assertEqual(view["summary"]["tools"], 20)
        self.assertEqual([g["id"] for g in view["groups"]], ["system", "tail", "compact", "render", "dream"])
        self.assertEqual([g["id"] for g in view["tools"]], ["chat", "dream"])
        # 全新的数据目录：看一眼不许种出任何东西（连 memory.db 都不许）
        self.assertFalse(empty.data_dir.exists(), "打开提示词页把 data/ 建出来了？")
        # 每一条都带稳定 id（下一步要做可编辑，靠它定位）
        for group in view["groups"]:
            for item in group["items"]:
                self.assertTrue(item["id"])
                self.assertTrue(item["text"])
        for group in view["tools"]:
            for item in group["items"]:
                self.assertEqual(item["id"], item["name"])
                self.assertTrue(item["description"])

    def test_prompts_endpoint_follows_the_interface_language(self) -> None:
        """切到英文后，这一页给的也是英文那一套（含提示词原文）。"""
        self._post("/api/settings/language", {"language": "en"})
        try:
            _status, body, _ctype = self._get("/api/prompts")
            view = json.loads(body)
            self.assertEqual(view["language"], "en")
            system = [i for g in view["groups"] for i in g["items"] if i["id"] == "SYSTEM_PROMPT"][0]
            self.assertNotIn("你是运行在 O₃ᶻᵒⁿᵉ", system["text"])
            self.assertIn("You are", system["text"])
        finally:
            self._post("/api/settings/language", {"language": "zh"})

    def test_prompts_update_and_revert_round_trip(self) -> None:
        """保存一条 → 页面看到"已修改" → 复原 → 回到内置（契约 docs/14 §4）。"""
        from harness import prompts as prompt_store

        self.addCleanup(prompt_store.configure, None)
        self.server.settings = self.settings
        # {version}/{model}（ADR-115）留着：这个用例验的是"改过→复原"的往返，
        # 不缺占位符才不该有警告；缺了会警告这件事由下面那条用例单独验。
        custom = "【我改的】{version}{model}{web_tools_text}{vault_text}{memory_text}"
        status, result = self._post(
            "/api/prompts/update",
            {"language": "zh", "id": "SYSTEM_PROMPT", "text": custom},
        )
        self.assertEqual(status, 200, result)
        self.assertTrue(result["stored"])
        self.assertTrue(result["overridden"])

        _status, body, _ctype = self._get("/api/prompts")
        view = json.loads(body)
        self.assertEqual(view["overrides"]["zh"], ["SYSTEM_PROMPT"])
        self.assertEqual(view["summary"]["overridden"], 1)
        item = [
            i for g in view["groups"] for i in g["items"] if i["id"] == "SYSTEM_PROMPT"
        ][0]
        self.assertEqual(item["text"], custom)
        self.assertTrue(item["overridden"])
        self.assertTrue(item["default_text"].startswith("你是运行在 O₃ᶻᵒⁿᵉ"))
        self.assertEqual(item["warnings"], [])

    def test_prompts_update_warns_when_identity_placeholders_are_dropped(self) -> None:
        """ADR-115：自报家门的 {version}/{model} 被自定义文本丢掉时只允许警告
        （不拒绝 —— 那是用户文本的合法选择），但警告必须真的出现。"""
        from harness import prompts as prompt_store

        self.addCleanup(prompt_store.configure, None)
        self.server.settings = self.settings
        status, result = self._post(
            "/api/prompts/update",
            {
                "language": "zh",
                "id": "SYSTEM_PROMPT",
                "text": "【我改的】{web_tools_text}{vault_text}{memory_text}",
            },
        )
        self.assertEqual(status, 200, result)
        _status, body, _ctype = self._get("/api/prompts")
        item = [
            i
            for g in json.loads(body)["groups"]
            for i in g["items"]
            if i["id"] == "SYSTEM_PROMPT"
        ][0]
        joined = " ".join(item["warnings"])
        self.assertIn("version", joined)
        self.assertIn("model", joined)

        status, result = self._post(
            "/api/prompts/revert", {"language": "zh", "id": "SYSTEM_PROMPT"}
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["cleared"], 1)
        _status, body, _ctype = self._get("/api/prompts")
        item = [
            i
            for g in json.loads(body)["groups"]
            for i in g["items"]
            if i["id"] == "SYSTEM_PROMPT"
        ][0]
        self.assertFalse(item["overridden"])
        self.assertTrue(item["text"].startswith("你是运行在 O₃ᶻᵒⁿᵉ"))

    def test_prompts_update_rejects_bad_input(self) -> None:
        from harness import prompts as prompt_store

        self.addCleanup(prompt_store.configure, None)
        self.server.settings = self.settings
        for payload, expected in (
            ({"language": "jp", "id": "SYSTEM_PROMPT", "text": "x"}, "不认识的语言"),
            ({"language": "zh", "id": "NOPE", "text": "x"}, "没有这条提示词"),
            ({"language": "zh", "id": "SYSTEM_PROMPT", "text": "  "}, "复原"),
            ({"language": "zh", "id": "SYSTEM_PROMPT", "text": "x" * 20_001}, "太长了"),
            ({"language": "zh", "id": "SYSTEM_PROMPT", "text": "{foo}"}, "{foo}"),
        ):
            with self.subTest(language=payload["language"], id=payload["id"], size=len(payload["text"])):
                status, result = self._post("/api/prompts/update", payload)
                self.assertEqual(status, 400)
                self.assertIn(expected, result["error"])
        self.assertFalse(
            (self.settings.data_dir / prompt_store.OVERRIDES_FILE).exists(),
            "被拒的保存不许留下文件",
        )

    def test_prompts_language_isolation_over_http(self) -> None:
        """**改中文不影响英文** —— 走真接口再验一遍（用户最担心的一条）。"""
        from harness import prompts as prompt_store

        self.addCleanup(prompt_store.configure, None)
        self.server.settings = self.settings
        self._post(
            "/api/prompts/update",
            {"language": "zh", "id": "TAIL_NOW", "text": "# 中文 {stamp}"},
        )
        self._post(
            "/api/prompts/update",
            {"language": "en", "id": "TAIL_NOW", "text": "# English {stamp}"},
        )
        self._post("/api/prompts/revert", {"language": "zh"})
        _status, body, _ctype = self._get("/api/prompts")
        view = json.loads(body)
        self.assertEqual(view["overrides"]["zh"], [])
        self.assertEqual(view["overrides"]["en"], ["TAIL_NOW"])
        self.assertTrue(prompt_store.bundle("en").TAIL_NOW.startswith("# English"))

    def test_prompts_reports_a_corrupt_override_file(self) -> None:
        from harness import prompts as prompt_store

        self.addCleanup(prompt_store.configure, None)
        self.server.settings = self.settings
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        (self.settings.data_dir / prompt_store.OVERRIDES_FILE).write_text(
            "{ 坏文件", encoding="utf-8"
        )
        status, body, _ctype = self._get("/api/prompts")
        self.assertEqual(status, 200)
        view = json.loads(body)
        self.assertTrue(view["store_error"], "坏文件必须报出来")
        self.assertEqual(view["summary"]["overridden"], 0)
        item = [
            i for g in view["groups"] for i in g["items"] if i["id"] == "SYSTEM_PROMPT"
        ][0]
        self.assertTrue(item["text"].startswith("你是运行在 O₃ᶻᵒⁿᵉ"))

    def test_vault_endpoint_is_read_only(self) -> None:
        """看一眼不能改磁盘：vault 目录不存在时，GET 也不该把它建出来。"""
        self.assertFalse(self.settings.vault_dir.exists())
        status, body, _ = self._get("/api/vault")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["check"]["exists"])
        self.assertFalse(self.settings.vault_dir.exists(), "GET 把 vault 目录建出来了？")

    def test_vault_view_has_a_tree_for_clicking_through(self) -> None:
        """界面要"逐层点开"，所以视图里直接给一棵建好的树（服务端建，顺序只有一份）。"""
        from harness.vault import VaultStore

        VaultStore(self.settings.vault_dir).write_file("README.md", "# 背景档案\n")
        self._seed_dream_run()
        view = json.loads(self._get("/api/vault")[1])
        tree = view["tree"]
        dirs = {node["name"]: node for node in tree if node["type"] == "dir"}
        self.assertEqual(set(dirs), {"entities", "sections"})
        self.assertEqual(dirs["entities"]["files"], 1)
        self.assertEqual(
            [child["path"] for child in dirs["entities"]["children"]],
            ["entities/小林.md"],
        )
        # 目录在前、文件在后；README 是根上唯一可点的文件（它存在时）
        kinds = [node["type"] for node in tree]
        self.assertEqual(kinds, ["file", "dir", "dir"])
        readme = [node for node in tree if node.get("is_readme")]
        self.assertEqual(len(readme), 1)
        self.assertEqual(readme[0]["path"], "README.md")
        # 树上的字数不能是占位 0（那是会骗人的数字）：README 正文 "# 背景档案" 9 字
        self.assertGreater(readme[0]["chars"], 0)

    def test_vault_tree_is_recursive_so_deeper_layouts_keep_working(self) -> None:
        """我在 ADR-067 里声明"树是递归的，哪天放宽层级界面不用改" —— 那就得能证明。

        直接用假的条目喂 `_vault_tree`（vault 的路径规范现在只允许两级，
        所以造不出三层文件；但树本身不该假设只有两级）。
        """
        from harness.web import _vault_tree

        class Entry:
            def __init__(self, path: str, title: str) -> None:
                self.path = path
                self.title = title
                self.kind = "note"
                self.chars = 3
                self.summary = "摘要"

        tree = _vault_tree(
            [
                Entry("sections/当月.md", "当月"),
                Entry("sections/archived_month/2026-09.md", "九月"),
                Entry("sections/archived_month/2026-08.md", "八月"),
                Entry("sections/raw/source_1/片段.md", "片段"),
            ],
            has_readme=False,
        )
        top = {node["name"]: node for node in tree if node["type"] == "dir"}
        self.assertEqual(set(top), {"sections"})
        sections = top["sections"]
        # 该目录下的文件总数要把更深层的算进来
        self.assertEqual(sections["files"], 4)
        kinds = [(node["type"], node.get("name") or node["path"]) for node in sections["children"]]
        # 目录在前、文件在后
        self.assertEqual(
            kinds,
            [
                ("dir", "archived_month"),
                ("dir", "raw"),
                ("file", "sections/当月.md"),
            ],
        )
        archived = sections["children"][0]
        self.assertEqual(archived["files"], 2)
        raw = sections["children"][1]
        self.assertEqual(raw["files"], 1)
        # 三层：raw -> source_1 -> 片段.md
        source = raw["children"][0]
        self.assertEqual(source["type"], "dir")
        self.assertEqual(source["name"], "source_1")
        self.assertEqual(
            [child["path"] for child in source["children"]],
            ["sections/raw/source_1/片段.md"],
        )

    def test_vault_view_tree_omits_readme_when_it_does_not_exist(self) -> None:
        from harness.vault import VaultStore

        VaultStore(self.settings.vault_dir).write_file("sections/口味.md", "手冲")
        tree = json.loads(self._get("/api/vault")[1])["tree"]
        self.assertEqual([node for node in tree if node.get("is_readme")], [])
        self.assertEqual([node["name"] for node in tree], ["sections"])

    # -- 原生 vault（ADR-104）----------------------------------------

    def _native_vault(self):
        from harness.vault import (
            Frontmatter,
            SectionSpec,
            SectionsManifest,
            VaultStore,
        )

        settings = test_settings(Path(self._tmp.name) / "native")
        store = VaultStore(settings.vault_dir)
        store.init_skeleton()
        store.write_manifest(
            SectionsManifest(
                inject_char_cap=6000,
                sections=(
                    SectionSpec("sections/personal_context.md", "Personal Context"),
                    SectionSpec("sections/earlier_months.md", "Earlier Months"),
                ),
            )
        )
        store.save_note(
            "sections/personal_context.md",
            "Hello [[project_photon]] and ![[raw/source_1/pic.png]].",
            Frontmatter(),
        )
        store.save_note(
            "entities/project_photon.md",
            "Body.",
            Frontmatter(name="project_photon", tags=("project",)),
        )
        store.write_file(
            "sections/archived_month/2026-09/this_month.md",
            "---\nmonth: 2026-09\n---\n\n# 2026-09\n\nOld month.",
        )
        raw_dir = settings.vault_dir / "raw/source_1"
        raw_dir.mkdir(parents=True)
        (raw_dir / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        store.rebuild_generated(today="2026-11-07", last_dream="2026-11-07")
        self.server.settings = settings
        return settings, store

    def test_memory_instruction_has_its_own_window_not_the_dream_tree(self) -> None:
        memory = MemoryStore(
            instruction_path=self.settings.memory_instruction_path,
            cursor_db_path=self.settings.memory_db_path,
        )
        try:
            memory.create("只应出现在记忆页的条目")
        finally:
            memory.close()

        view = json.loads(self._get("/api/vault")[1])
        self.assertNotIn(
            "memory_instruction.md",
            [item["path"] for item in view["files"]],
        )

        def paths(nodes):
            for node in nodes:
                if node.get("type") == "file":
                    yield node["path"]
                else:
                    yield from paths(node.get("children") or [])

        self.assertNotIn("memory_instruction.md", list(paths(view["tree"])))
        memories = json.loads(self._get("/api/memories")[1])
        self.assertEqual(memories["items"][0]["content"], "只应出现在记忆页的条目")

    def test_native_vault_view_tree_nested_and_raw(self) -> None:
        settings, store = self._native_vault()
        view = json.loads(self._get("/api/vault")[1])
        self.assertTrue(view["native"])
        self.assertEqual(view["raw_files"], 1)
        self.assertEqual(view["manifest"]["inject_char_cap"], 6000)
        sections = next(
            node for node in view["tree"] if node.get("type") == "dir" and node["name"] == "sections"
        )
        archived = next(
            node for node in sections["children"] if node.get("name") == "archived_month"
        )
        self.assertEqual(archived["children"][0]["name"], "2026-09")
        self.assertTrue(
            any(
                item["path"] == "sections/archived_month/2026-09/this_month.md"
                for item in (view["files"] or [])
            )
        )
        # 实体视图带原生字段
        status, body, _ = self._get("/api/vault/file?path=entities/project_photon.md")
        self.assertEqual(status, 200)
        entity = json.loads(body)
        self.assertEqual(entity["name"], "project_photon")
        self.assertEqual(entity["tags"], ["project"])
        self.assertTrue(entity["editable"])
        self.assertIn("last_updated", entity)
        # 嵌套 section 也能读
        status, body, _ = self._get(
            "/api/vault/file?path=sections/archived_month/2026-09/this_month.md"
        )
        self.assertEqual(status, 200)
        # raw 图片（200 / 400 / 404）
        status, payload, content_type = self._get(
            "/api/vault/raw?path=raw/source_1/pic.png"
        )
        self.assertEqual(status, 200)
        self.assertIn("image/png", content_type)
        self.assertTrue(payload.startswith(b"\x89PNG"))
        status, _body, _ct = self._get("/api/vault/raw?path=raw/source_1/%2E%2E/pic.png")
        self.assertIn(status, (400, 404))
        status, _body, _ct = self._get("/api/vault/raw?path=raw/source_1/gone.png")
        self.assertEqual(status, 404)

    def test_native_generated_files_are_read_only_but_sections_rebuild(self) -> None:
        settings, store = self._native_vault()
        status, body, _ = self._get("/api/vault/file?path=README.md")
        self.assertEqual(status, 200)
        readme = json.loads(body)
        self.assertFalse(readme["editable"])
        self.assertTrue(readme["is_generated"])

        status, result = self._post(
            "/api/vault/file",
            {"path": "README.md", "body": "nope", "mode": "update"},
        )
        self.assertEqual(status, 400)
        self.assertIn("生成", result["error"])

        status, body, _ = self._get("/api/vault/file?path=sections/personal_context.md")
        section = json.loads(body)
        status, result = self._post(
            "/api/vault/file",
            {
                "path": "sections/personal_context.md",
                "body": "Changed body [[project_photon]].",
                "mode": "update",
                "expect_hash": section["hash"],
                "title": "Personal Context",
            },
        )
        self.assertEqual(status, 200, result)
        self.assertIn("Changed body", (settings.vault_dir / "README.md").read_text(encoding="utf-8"))
        self.assertEqual(store.check()["problems"], [])


    def test_vault_file_endpoint_returns_content_and_neighborhood(self) -> None:
        """点开一个档案：全文 + 元信息 + "它指向谁 / 谁指向它"。"""
        from harness.vault import VaultStore

        store = VaultStore(self.settings.vault_dir)
        store.write_file(
            "entities/小林.md",
            "---\ntitle: 小林\nkind: person\ntags: [用户]\n---\n\n见 [[小张]] 与 [[偏好]]。\n",
        )
        store.write_file("entities/小张.md", "---\ntitle: 小张\n---\n\n[[小林]] 的伙伴。\n")
        store.write_file("sections/偏好.md", "---\ntitle: 偏好\n---\n\n只喝浅烘。\n")
        store.write_file("entities/无关.md", "---\ntitle: 无关\n---\n\n谁也不提。\n")

        status, body, _ = self._get("/api/vault/file?path=entities/%E5%B0%8F%E6%9E%97.md")
        self.assertEqual(status, 200)
        view = json.loads(body)
        self.assertEqual(view["path"], "entities/小林.md")
        self.assertEqual(view["title"], "小林")
        self.assertEqual(view["kind"], "person")
        self.assertEqual(view["tags"], ["用户"])
        self.assertIn("[[小张]]", view["body"])
        self.assertFalse(view["truncated"])
        # 它指向谁：两条链接都解析到了真实文件
        self.assertEqual(
            sorted(item["path"] for item in view["links"]),
            ["entities/小张.md", "sections/偏好.md"],
        )
        # 谁指向它：小张指向小林
        self.assertEqual([item["path"] for item in view["backlinks"]], ["entities/小张.md"])

    def test_vault_file_endpoint_accepts_a_bare_name(self) -> None:
        """`?path=小林` 也要能用 —— 与模型侧同一套"宽松理解"，界面不必自己补目录。"""
        from harness.vault import VaultStore

        VaultStore(self.settings.vault_dir).write_file(
            "entities/小林.md", "---\ntitle: 小林\n---\n\n内容\n"
        )
        status, body, _ = self._get("/api/vault/file?path=%E5%B0%8F%E6%9E%97")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["path"], "entities/小林.md")

    def test_vault_file_endpoint_reads_the_readme(self) -> None:
        from harness.vault import VaultStore

        VaultStore(self.settings.vault_dir).write_file(
            "README.md", "# 背景档案\n\n- 小林\n"
        )
        status, body, _ = self._get("/api/vault/file?path=README.md")
        self.assertEqual(status, 200)
        view = json.loads(body)
        self.assertTrue(view["is_readme"])
        self.assertIn("背景档案", view["body"])

    def test_vault_file_endpoint_reports_missing_and_bad_paths(self) -> None:
        """不存在的档案是 404；非法路径是 400（两者不能混着说）。"""
        from harness.vault import VaultStore

        VaultStore(self.settings.vault_dir).write_file("sections/口味.md", "手冲")

        status, body, _ = self._get("/api/vault/file?path=sections/nope.md")
        self.assertEqual(status, 404)
        self.assertIn("没有这个档案", json.loads(body)["error"])

        for bad in (
            "../../../etc/passwd",
            "../../memory.db",
            "C:/Windows/system32/config",
            "sections/sub/deep.md",
            "other/x.md",
        ):
            status, body, _ = self._get(
                "/api/vault/file?path=" + urllib.parse.quote(bad, safe="")
            )
            self.assertEqual(status, 400, bad)
            self.assertIn("error", json.loads(body), bad)

        # 已经编码过一次的 `%2F` 不是穿越：它落到名字里就只是个奇怪的字符，
        # 所以"没有这个档案"（404）才是对的 —— 不能把它当成越权去报 400。
        status, _body, _ = self._get("/api/vault/file?path=..%252F..%252Fmemory.db")
        self.assertEqual(status, 404)

        status, body, _ = self._get("/api/vault/file")
        self.assertEqual(status, 400)
        self.assertIn("缺少 path", json.loads(body)["error"])

    def test_vault_file_endpoint_is_read_only(self) -> None:
        """点开一个不存在的档案不能把 vault 目录建出来。"""
        self.assertFalse(self.settings.vault_dir.exists())
        status, _, _ = self._get("/api/vault/file?path=sections/nope.md")
        self.assertEqual(status, 404)
        self.assertFalse(self.settings.vault_dir.exists())

    # -- 界面上的档案新建 / 编辑 / 删除（ADR-070）--------------------

    def _write_file(self, **payload):
        return self._post("/api/vault/file", payload)

    def test_vault_file_view_carries_a_hash(self) -> None:
        """界面靠这个指纹发现"你编辑期间档案被改过"，所以它必须真的跟着内容变。"""
        from harness.vault import VaultStore

        store = VaultStore(self.settings.vault_dir)
        store.write_file("sections/口味.md", "第一版\n")
        first = json.loads(
            self._get("/api/vault/file?path=sections/%E5%8F%A3%E5%91%B3.md")[1]
        )
        self.assertTrue(first["hash"])

        store.write_file("sections/口味.md", "第二版\n")
        second = json.loads(
            self._get("/api/vault/file?path=sections/%E5%8F%A3%E5%91%B3.md")[1]
        )
        self.assertNotEqual(first["hash"], second["hash"])

    def test_create_and_update_a_vault_file(self) -> None:
        status, result = self._write_file(
            mode="create",
            path="entities/小林.md",
            body="初二学生，和 [[小张]] 一起做项目。",
            title="小林",
            kind="person",
            tags=["用户", "同学"],
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["action"], "created")
        self.assertEqual(result["path"], "entities/小林.md")
        self.assertEqual(result["tags"], ["用户", "同学"])
        self.assertIn("check", result, "写完要顺手把结构校验带回来")

        text = (self.settings.vault_dir / "entities" / "小林.md").read_text(encoding="utf-8")
        self.assertIn("title: 小林", text)
        self.assertIn("[[小张]]", text)
        self.assertIn("updated: ", text)

        # 改一次：正文换掉、只给 kind，别的字段不该被清空
        status, result = self._write_file(
            mode="update", path="entities/小林.md", body="改过的正文。", kind="person"
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["action"], "updated")
        self.assertIn("改过的正文", result["body"])
        self.assertEqual(result["tags"], ["用户", "同学"], "没提到的字段不该被清掉")
        self.assertEqual(result["title"], "小林")

    def test_hand_written_frontmatter_keys_survive_a_ui_edit(self) -> None:
        """你手工加的键（工具不认识的那种）不能在界面保存一次就没了。"""
        from harness.vault import VaultStore

        VaultStore(self.settings.vault_dir).write_file(
            "entities/小林.md",
            "---\ntitle: 小林\npinned: true\nnote: 我手写的\n---\n\n旧正文\n",
        )
        status, result = self._write_file(
            mode="update", path="entities/小林.md", body="新正文"
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["extra"].get("pinned"), "true")
        self.assertEqual(result["extra"].get("note"), "我手写的")

    def test_create_refuses_to_clobber_an_existing_file(self) -> None:
        from harness.vault import VaultStore

        VaultStore(self.settings.vault_dir).write_file("entities/小林.md", "原有内容\n")
        status, result = self._write_file(
            mode="create", path="entities/小林.md", body="新内容"
        )
        self.assertEqual(status, 409)
        self.assertIn("已经有一份", result["error"])
        # 带上**规范化之后**的路径：界面要拿它去打开那一份，而"裸名字"它补不出来
        self.assertEqual(result["path"], "entities/小林.md")
        self.assertTrue(result["exists"])
        self.assertIn(
            "原有内容",
            (self.settings.vault_dir / "entities" / "小林.md").read_text(encoding="utf-8"),
        )

    def test_update_refuses_a_missing_file(self) -> None:
        """拼错路径时**不能**顺手建出一份新档案。"""
        status, result = self._write_file(
            mode="update", path="entities/拼错了.md", body="内容"
        )
        self.assertEqual(status, 404)
        self.assertFalse((self.settings.vault_dir / "entities" / "拼错了.md").exists())

    def test_stale_hash_is_refused_and_the_new_content_comes_back(self) -> None:
        """**最要紧的一条**：你看着旧内容保存时，不能把别人刚写的悄悄覆盖掉。"""
        from harness.vault import VaultStore

        store = VaultStore(self.settings.vault_dir)
        store.write_file("sections/口味.md", "第一版\n")
        loaded = json.loads(
            self._get("/api/vault/file?path=sections/%E5%8F%A3%E5%91%B3.md")[1]
        )

        # 模型（或另一个窗口）在你编辑期间改了它
        store.write_file("sections/口味.md", "模型刚写的新版\n")

        status, result = self._write_file(
            mode="update",
            path="sections/口味.md",
            body="我在旧版基础上改的",
            expect_hash=loaded["hash"],
        )
        self.assertEqual(status, 409)
        self.assertIn("被改过", result["error"])
        self.assertIn("模型刚写的新版", result["body"], "要把最新内容一起带回来")
        self.assertIn(
            "模型刚写的新版",
            (self.settings.vault_dir / "sections" / "口味.md").read_text(encoding="utf-8"),
            "被拒之后磁盘上必须还是最新的那份",
        )

        # 拿新指纹再存一次就成了
        status, result = self._write_file(
            mode="update",
            path="sections/口味.md",
            body="我看过新版之后的改动",
            expect_hash=result["hash"],
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["action"], "updated")

    def test_bad_paths_and_names_are_rejected_with_the_vault_message(self) -> None:
        for bad, label in (
            ("../secret.md", "穿越"),
            ("/etc/passwd", "绝对路径"),
            ("other/x.md", "目录不对"),
            ("sections/sub/deep.md", "三层"),
            ("entities/NUL.md", "设备名"),
        ):
            status, result = self._write_file(mode="create", path=bad, body="内容")
            self.assertEqual(status, 400, f"{label} 应当是 400：{result}")
            self.assertIn("error", result)
        self.assertIn("缺少 path", self._write_file(mode="create", body="无路径")[1]["error"])
        self.assertIn(
            "mode 只能是",
            self._write_file(mode="乱写", path="sections/a.md", body="x")[1]["error"],
        )

    def test_case_only_clash_is_refused(self) -> None:
        """大小写不敏感的系统上，大小写不同的两个名字是同一份 —— 不能悄悄覆盖。"""
        from harness.vault import VaultStore

        VaultStore(self.settings.vault_dir).write_file("entities/foo.md", "原有\n")
        status, result = self._write_file(mode="create", path="entities/Foo.md", body="新的")
        self.assertEqual(status, 400)
        self.assertTrue(
            "大小写" in result["error"] or "已经有一份" in result["error"], result["error"]
        )

    def test_over_long_body_is_refused(self) -> None:
        status, result = self._write_file(
            mode="create",
            path="sections/太长.md",
            body="字" * (self.settings.vault_write_max_chars + 100),
        )
        self.assertEqual(status, 400)
        self.assertIn("上限", result["error"])

    def test_readme_can_be_edited_but_not_muted(self) -> None:
        from harness.vault import VaultStore

        VaultStore(self.settings.vault_dir).write_file("sections/口味.md", "手冲\n")
        # 还没有 README 时可以先建一份（界面上"新建 README"就是这条路径）
        status, result = self._write_file(
            mode="create", path="README.md", body="# 背景档案\n\n- 口味\n"
        )
        self.assertEqual(status, 200, result)
        self.assertTrue(result["is_readme"])
        self.assertEqual(result["action"], "created")

        status, result = self._write_file(
            mode="update", path="README.md", body="# 背景档案\n\n- 口味（浅烘）\n"
        )
        self.assertEqual(status, 200, result)
        readme, _ = VaultStore(self.settings.vault_dir).readme_text()
        self.assertIn("浅烘", readme)

        # 但那个会"永久静音"整个档案层的标记不能写进去
        status, result = self._write_file(
            mode="update", path="README.md", body="内容", status="placeholder"
        )
        self.assertEqual(status, 400)
        self.assertIn("placeholder", result["error"])

    def test_delete_a_vault_file_reports_dangling_links(self) -> None:
        from harness.vault import VaultStore

        store = VaultStore(self.settings.vault_dir)
        store.write_file("entities/小张.md", "[[小林]] 的伙伴。\n")
        store.write_file("entities/小林.md", "本人。\n")
        loaded = json.loads(
            self._get("/api/vault/file?path=entities/%E5%B0%8F%E6%9E%97.md")[1]
        )

        status, result = self._post(
            "/api/vault/file/delete",
            {"path": "entities/小林.md", "expect_hash": loaded["hash"]},
        )
        self.assertEqual(status, 200, result)
        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], "entities/小林.md")
        self.assertEqual(result["dangling"], ["entities/小张.md"], "谁指向它要说出来")
        self.assertFalse((self.settings.vault_dir / "entities" / "小林.md").exists())
        self.assertIn("check", result)

    def test_delete_refuses_readme_missing_files_and_stale_hash(self) -> None:
        from harness.vault import VaultStore

        store = VaultStore(self.settings.vault_dir)
        store.write_file("README.md", "# 背景档案\n")
        store.write_file("entities/小林.md", "本人。\n")

        status, result = self._post("/api/vault/file/delete", {"path": "README.md"})
        self.assertEqual(status, 400)
        self.assertIn("不能删", result["error"])
        self.assertTrue((self.settings.vault_dir / "README.md").is_file())

        status, _result = self._post(
            "/api/vault/file/delete", {"path": "entities/没有.md"}
        )
        self.assertEqual(status, 404)

        store.write_file("entities/小林.md", "被改过了。\n")
        status, _result = self._post(
            "/api/vault/file/delete",
            {"path": "entities/小林.md", "expect_hash": "0123456789abcdef"},
        )
        self.assertEqual(status, 409)
        self.assertTrue((self.settings.vault_dir / "entities" / "小林.md").is_file())

    def test_writes_are_refused_while_a_consolidation_is_running(self) -> None:
        """整理会按它开跑时读到的内容重写档案 —— 你在这期间的手改会被无声覆盖。

        所以干脆不并发：锁被拿着的时候，写接口一律 409 并说清为什么。
        """
        lock = self.server.dream_lock
        lock.acquire()
        try:
            status, result = self._write_file(
                mode="create", path="entities/小林.md", body="内容"
            )
            self.assertEqual(status, 409)
            self.assertIn("整理正在运行", result["error"])
            self.assertFalse((self.settings.vault_dir / "entities" / "小林.md").exists())
        finally:
            lock.release()

        # 锁放开之后照常能写
        status, _result = self._write_file(
            mode="create", path="entities/小林.md", body="内容"
        )
        self.assertEqual(status, 200)

    def test_vault_writes_need_the_post_gates(self) -> None:
        """改档案的接口和别的 POST 一样要过同源 + JSON 两道闸。"""
        import urllib.error

        request = urllib.request.Request(
            self.base + "/api/vault/file",
            data=json.dumps({"path": "sections/a.md", "body": "x"}).encode("utf-8"),
            headers={"Content-Type": "text/plain"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=30)
        self.assertEqual(ctx.exception.code, 400)
        self.assertFalse(self.settings.vault_dir.exists())

    def test_saving_an_empty_body_is_allowed_but_flagged(self) -> None:
        """改到一半先存个草稿是正常需求，所以**允许**存空正文。

        但"有元信息、正文空"正是手滑删掉正文就保存的形状，所以结构校验必须报出来 ——
        否则它会静静地躺在那儿，谁也不知道。
        """
        status, result = self._write_file(
            mode="create", path="entities/草稿.md", body="先占个位", title="草稿"
        )
        self.assertEqual(status, 200, result)

        status, result = self._write_file(
            mode="update", path="entities/草稿.md", body="   ", title="草稿"
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["body"].strip(), "")
        self.assertTrue(
            any("正文是空的" in item for item in result["check"]["problems"]),
            result["check"]["problems"],
        )

    def test_clearing_a_field_in_the_ui_actually_clears_it(self) -> None:
        """界面把某个框清空 = 要抹掉那个字段。

        合并语义默认"空 = 保持原样"，所以这里必须显式说一声 ——
        否则用户清掉 `status: 进行中` 之后文件里还留着它，模型下一轮还会读到
        （这是**内容正确性**问题，不是外观问题）。
        """
        from harness.vault import VaultStore

        store = VaultStore(self.settings.vault_dir)
        store.write_file(
            "entities/项目.md",
            "---\ntitle: 项目\nkind: project\nstatus: 进行中\ntags: [a, b]\n---\n\n正文\n",
        )
        status, result = self._write_file(
            mode="update",
            path="entities/项目.md",
            body="正文",
            title="项目",
            kind="",
            status="",
            tags=[],
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["title"], "项目", "没清的那个字段要留着")
        self.assertEqual(result["kind"], "")
        self.assertEqual(result["status"], "")
        self.assertEqual(result["tags"], [])

        # 再存一次同样不带这些键：不能把它们"复活"
        status, result = self._write_file(
            mode="update", path="entities/项目.md", body="正文", title="项目"
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["kind"], "")
        self.assertEqual(result["status"], "")

    def test_omitting_a_field_keeps_it(self) -> None:
        """**没提到**的字段要保持原样（与模型侧一致：它不提就是不改）。

        与上一条的区别是"键在不在请求里"：空值是"抹掉"，不出现是"不动"。
        """
        from harness.vault import VaultStore

        VaultStore(self.settings.vault_dir).write_file(
            "entities/项目.md", "---\ntitle: 项目\nstatus: 进行中\n---\n\n正文\n"
        )
        status, result = self._write_file(
            mode="update", path="entities/项目.md", body="只改正文"
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["title"], "项目")
        self.assertEqual(result["status"], "进行中")

    def test_dream_format_needs_the_confirm_word_and_clears_only_dream(self) -> None:
        """格式化 dream 层：要确认词；清掉 dream 那三样；**别的一律不动**。"""
        from harness.dream import dream_format_confirm
        from harness.reset import dream_state

        # 确认词跟着界面语言走（ADR-085）：中文「格式化」/ 英文 `format`。
        # 取的是**这一刻**的那一个，所以在模块顶层 import 成常量是不对的。
        DREAM_FORMAT_CONFIRM = dream_format_confirm()

        # 造一点 dream 层 + 一点"不该被碰"的东西
        log = EventLog(self.settings.sessions_dir / "s1.jsonl", "s1")
        log.append("user_message", text="你好")
        log.close()
        memory = open_memory(self.settings)
        try:
            memory.create("用户在做记忆系统")
            memory.advance_cursor("s1", 1, "dream-x")
        finally:
            memory.close()
        from harness.vault import VaultStore

        VaultStore(self.settings.vault_dir).write_file("entities/小林.md", "手写的\n")
        self._seed_dream_run()

        session_before = (self.settings.sessions_dir / "s1.jsonl").read_bytes()
        providers = self.settings.data_dir / "providers.json"
        providers.write_text('{"providers": [{"id": "x"}]}', encoding="utf-8")

        # ① 没有确认词 / 确认词不对 → 400，而且什么都不做
        for payload in (
            {},
            {"confirm": "yes"},
            {"confirm": "格式化dream"},  # 连在一起不算
            {"confirm": "格 式 化"},      # 中间插空格不算
            {"confirm": "格式化!"},
        ):
            status, result = self._post("/api/dream/format", payload)
            self.assertEqual(status, 400, payload)
            self.assertEqual(result["confirm_word"], DREAM_FORMAT_CONFIRM)
        self.assertEqual(dream_state(self.settings)["vault_files"], 2, "被拒时不该动")

        # 前后空白是**容忍**的：人会连空格一起复制，那不影响"他确实打了这三个字"
        status, result = self._post("/api/dream/format", {"confirm": "  格式化  "})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["before"]["vault_files"], 2, "这一次才真的清")

        # ② 确认词一字不差 → 200
        status, result = self._post(
            "/api/dream/format", {"confirm": DREAM_FORMAT_CONFIRM}
        )
        self.assertEqual(status, 200, result)
        self.assertTrue(result["ok"])
        self.assertEqual(result["after"]["vault_files"], 0)
        self.assertEqual(result["after"]["dream_runs"], 0)
        self.assertEqual(result["after"]["cursor_sessions"], 0)
        self.assertEqual(result["leftovers"], [])
        # 幂等：再来一次不报错
        status, result = self._post(
            "/api/dream/format", {"confirm": DREAM_FORMAT_CONFIRM}
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["before"]["vault_files"], 0)

        # ③ 别的一律没动
        self.assertEqual((self.settings.sessions_dir / "s1.jsonl").read_bytes(), session_before)
        self.assertEqual(providers.read_text(encoding="utf-8"), '{"providers": [{"id": "x"}]}')
        memory = open_memory(self.settings)
        try:
            self.assertEqual(memory.stats()["active"], 1, "记忆条目被动过")
        finally:
            memory.close()
        # 初始化出来了
        self.assertTrue((self.settings.vault_dir / "sections").is_dir())

    def test_dream_format_refuses_while_a_run_is_in_progress(self) -> None:
        from harness.dream import dream_format_confirm

        DREAM_FORMAT_CONFIRM = dream_format_confirm()

        lock = self.server.dream_lock
        lock.acquire()
        try:
            status, result = self._post(
                "/api/dream/format", {"confirm": DREAM_FORMAT_CONFIRM}
            )
            self.assertEqual(status, 409)
            self.assertIn("正在运行", result["error"])
        finally:
            lock.release()

    def test_dream_endpoint_rejects_dry_run_instead_of_ignoring_it(self) -> None:
        """**这条是安全相关的**：还缓存着旧页面的浏览器会 POST `dry_run: true`，
        并**以为不会改动任何东西**。如果我们只是把那个字段丢掉，它就变成了一次
        **真整理** —— 用户以为在预演、实际在写档案（而且不可逆）。宁可报错。
        """
        status, result = self._post("/api/dream", {"dry_run": True})
        self.assertEqual(status, 400)
        self.assertIn("预演已经取消", result["error"])
        # 真整理那条路不受影响（这里没有待整理，所以走到"什么都没做"）
        status, body, _ = self._get("/api/dreams")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["runs"], [], "被拒的请求不该留下运行记录")

    def test_dream_runs_list_and_detail(self) -> None:
        run_id = self._seed_dream_run()

        status, body, _ = self._get("/api/dreams")
        self.assertEqual(status, 200)
        runs = json.loads(body)["runs"]
        self.assertEqual([run["id"] for run in runs], [run_id])
        # 列表只要摘要，不带整份差异
        self.assertEqual(runs[0]["changed"], 1)
        self.assertNotIn("diff", runs[0])

        status, body, _ = self._get(f"/api/dreams/{run_id}")
        self.assertEqual(status, 200)
        detail = json.loads(body)
        self.assertEqual(detail["summary"], "新建了小林这份档案。")
        self.assertEqual(detail["advanced"], {"s-1": 12})
        self.assertEqual([row["path"] for row in detail["diff"]["added"]], ["entities/小林.md"])
        # ADR-065：没有回滚，所以就没有 can_undo 这个字段了
        self.assertNotIn("can_undo", detail)
        self.assertIn("problems", detail["check"])

        status, _body, _ = self._get("/api/dreams/dream-nope")
        self.assertEqual(status, 404)

    def test_dream_undo_endpoint_is_gone(self) -> None:
        """回滚接口**不存在了**（ADR-065），所以是 404 而不是 400 ——
        与"参数不对"是两回事（同 ADR-058 删别名接口的先例）。

        响应里要写清"为什么没有"，否则用户会以为程序坏了。
        """
        run_id = self._seed_dream_run()
        for target in (run_id, "dream-nope"):
            status, result = self._post(f"/api/dreams/{target}/undo", {})
            self.assertEqual(status, 404, target)
            self.assertIn("回滚", result["error"])
            self.assertIn("ADR-065", result["error"])
        # 档案当然还在（这个接口没有副作用）
        self.assertTrue((self.settings.vault_dir / "entities" / "小林.md").is_file())

    # -- SSE ---------------------------------------------------------

    def test_chat_streams_and_reports_backend_failure(self) -> None:
        """后端连不通时，SSE 仍要完整走完并把错误如实推给前端，而不是静默断流。"""
        request = urllib.request.Request(
            self.base + "/api/chat",
            data=json.dumps({"text": "你好"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as resp:
            self.assertIn("text/event-stream", resp.headers.get("Content-Type", ""))
            body = resp.read().decode("utf-8")

        self.assertIn("event: session", body)
        self.assertIn("event: notice", body)
        self.assertIn("网络错误", body)
        self.assertIn("event: done", body)
        # 会话 id 必须回传，前端才知道把后续消息接到哪里
        session_event = [
            line for line in body.split("\n") if line.startswith("data: ") and "session_id" in line
        ]
        self.assertTrue(session_event)
        self.assertTrue(json.loads(session_event[0][6:])["session_id"])

    def test_dream_without_pending_does_not_need_a_provider(self) -> None:
        """**没有待整理的记录时，整理请求不该碰接口配置。**

        这条路径是唯一不需要接口就能走通的整理路径（没有会话 → 没有活干），
        所以顺序必须是"先看有没有活干，再解析接口"。第一版把顺序写反了：
        没有待整理 + 没配接口时会报"没有可用接口"，把用户引到一个不相干的设置页。

        而且这条路径**没有任何测试走过** —— 结果 `_run_dream()` 里少了一个 import
        都没被发现，直到真跑才 `NameError`（实测报错：summarize_pending is not defined）。
        这个测试就是那次事故的补丁。
        """
        empty = test_settings(Path(self._tmp.name) / "empty")
        self.server.settings = empty

        request = urllib.request.Request(
            self.base + "/api/dream",
            data=json.dumps({}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as resp:
            self.assertIn("text/event-stream", resp.headers.get("Content-Type", ""))
            body = resp.read().decode("utf-8")

        self.assertIn("event: dream_start", body)
        self.assertIn("event: dream_done", body)
        done = [
            json.loads(line[6:])
            for line in body.split("\n")
            if line.startswith("data: ") and "nothing_pending" in line
        ]
        self.assertTrue(done, "应当回一个 skipped 的 dream_done")
        self.assertTrue(done[0]["skipped"])
        self.assertEqual(done[0]["stop_reason"], "nothing_pending")
        # 关键：**没有**因为"没配接口"而报错
        self.assertNotIn("还没有配置任何接口", body)
        # 也没有留下任何运行记录（没花钱的事不该进历史）
        self.assertFalse(self.settings.dreams_dir.exists() and any(
            self.settings.dreams_dir.glob("dream-*.json")
        ))

    def test_dream_uses_the_saved_model_setting(self) -> None:
        """整理模型来自**设置**（ADR-103），不是聊天页当前选中的那个。"""
        from harness.providers.store import ProviderStore
        from harness.runtime import GenerationChoice, ResolvedProvider

        log = EventLog(
            self.settings.sessions_dir / "s-dream-model.jsonl", "s-dream-model"
        )
        log.append("user_message", text="你好")
        log.close()

        status, result = self._post(
            "/api/providers/dream",
            {"provider": "deepseek", "model": "deepseek-v4-pro"},
        )
        self.assertEqual(status, 200, result)

        store = ProviderStore(self.settings.data_dir / "providers.json")
        profile = store.get("deepseek")
        assert profile is not None
        resolved = ResolvedProvider(
            choice=GenerationChoice(
                provider_id="deepseek",
                model="deepseek-v4-pro",
                effort="",
                label="DeepSeek",
            ),
            profile=profile,
            settings=test_settings(Path(self._tmp.name), model="deepseek-v4-pro"),
            provider=FakeProvider([]),
        )

        with mock.patch(
            "harness.web.resolve_dream_provider", return_value=resolved
        ) as patched:
            request = urllib.request.Request(
                self.base + "/api/dream",
                data=json.dumps({}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=60) as resp:
                body = resp.read().decode("utf-8")

        self.assertTrue(patched.called, "应当走「Dream 设置」那条解析路径")
        self.assertIn('"model": "deepseek-v4-pro"', body)
        # 运行记录里也记下了这次用的是哪个模型（事后能回答"档案是谁整理的"）
        records = sorted(self.settings.dreams_dir.glob("dream-*.json"))
        self.assertTrue(records)
        record = json.loads(records[-1].read_text(encoding="utf-8"))
        self.assertEqual(record["generation"]["model"], "deepseek-v4-pro")

    def test_dream_payload_override_still_wins_for_old_pages(self) -> None:
        """旧页面（缓存着旧 app.js）仍会带 provider/model；不能悄悄忽略它。

        新前端已经不发了，但"请求里写了却不用"等于在用户不知情时换模型。
        """
        from harness.providers.store import ProviderStore
        from harness.runtime import GenerationChoice, ResolvedProvider

        log = EventLog(
            self.settings.sessions_dir / "s-dream-override.jsonl", "s-dream-override"
        )
        log.append("user_message", text="你好")
        log.close()

        store = ProviderStore(self.settings.data_dir / "providers.json")
        profile = store.get("deepseek")
        assert profile is not None
        resolved = ResolvedProvider(
            choice=GenerationChoice(
                provider_id="deepseek",
                model="deepseek-flash",
                effort="",
                label="DeepSeek",
            ),
            profile=profile,
            settings=self.settings,
            provider=FakeProvider([]),
        )

        with mock.patch(
            "harness.web.resolve_provider", return_value=resolved
        ) as old_path, mock.patch(
            "harness.web.resolve_dream_provider"
        ) as saved_path:
            request = urllib.request.Request(
                self.base + "/api/dream",
                data=json.dumps(
                    {"provider": "deepseek", "model": "deepseek-flash"}
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=60) as resp:
                resp.read()

        self.assertTrue(old_path.called)
        self.assertFalse(saved_path.called)

    def test_dream_with_pending_but_no_provider_says_so(self) -> None:
        """反过来：**真的有待整理**但没有接口时，必须明说是接口的问题。"""
        from harness.events import EventLog

        empty = test_settings(Path(self._tmp.name) / "empty2")
        log = EventLog(empty.sessions_dir / "s-1.jsonl", "s-1")
        log.append("user_message", text="你好")
        log.append("assistant_message", message={"content": "嗯"})
        log.close()
        self.server.settings = empty

        status, result = self._post("/api/dream", {})
        self.assertEqual(status, 400)
        self.assertIn("还没有配置任何接口", result["error"])

    def test_chat_without_any_provider_is_rejected_readably(self) -> None:
        """一个接口都没有时，报错要能告诉用户下一步做什么。"""
        empty = test_settings(Path(self._tmp.name) / "empty")
        self.server.settings = empty
        status, result = self._post("/api/chat", {"text": "你好"})
        self.assertEqual(status, 400)
        self.assertIn("还没有配置任何接口", result["error"])
        self.assertIn("设置 → 接口", result["error"])

    def test_chat_rejects_empty_text(self) -> None:
        self.server.settings = self.settings
        status, result = self._post("/api/chat", {"text": "   "})
        self.assertEqual(status, 400)

    def test_busy_session_returns_409(self) -> None:
        """同一会话同时只允许一轮生成，否则两份日志会交错写坏历史。"""
        lock = self.server.session_lock("busy-session")
        lock.acquire()
        try:
            status, result = self._post(
                "/api/chat", {"session_id": "busy-session", "text": "你好"}
            )
            self.assertEqual(status, 409)
            self.assertIn("正在生成", result["error"])
        finally:
            lock.release()


    # -- 多供应商（M6）-----------------------------------------------

    def test_providers_endpoint_masks_keys(self) -> None:
        status, body, _ = self._get("/api/providers")
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertTrue(state["providers"])
        self.assertTrue(state["presets"])
        self.assertTrue(state["models"], "已配置的接口应该摊平成可选的模型列表")

        blob = json.dumps(state, ensure_ascii=False)
        self.assertNotIn("test-key", blob, "完整 key 绝不能出现在响应里")
        self.assertTrue(state["providers"][0]["has_key"])
        # 整理模型（ADR-103）：默认是"没设过"，前端据此显示「跟随默认」
        self.assertEqual(
            state["dream"], {"provider": "", "model": "", "effort": ""}
        )

    def test_provider_save_default_and_delete(self) -> None:
        status, result = self._post(
            "/api/providers/save", {"id": "zhipu", "preset": "zhipu", "api_key": "zk"}
        )
        self.assertEqual(status, 200)
        ids = [p["id"] for p in result["state"]["providers"]]
        self.assertIn("zhipu", ids)

        # 新加的接口还没模型，所以先手动写一个
        status, result = self._post(
            "/api/providers/save", {"id": "zhipu", "models": "glm-a, glm-b"}
        )
        zhipu = next(p for p in result["state"]["providers"] if p["id"] == "zhipu")
        self.assertEqual(zhipu["models"], ["glm-a", "glm-b"])

        status, result = self._post(
            "/api/providers/default", {"provider": "zhipu", "model": "glm-a"}
        )
        self.assertEqual(result["default"]["provider"], "zhipu")
        self.assertEqual(result["default"]["model"], "glm-a")

        status, result = self._post("/api/providers/delete", {"id": "zhipu"})
        self.assertNotIn("zhipu", [p["id"] for p in result["state"]["providers"]])

    def test_provider_save_keeps_existing_key_when_blank(self) -> None:
        """前端拿不到完整 key，所以"留空"必须表示不改动，而不是清空。"""
        self._post("/api/providers/save", {"id": "deepseek", "label": "改名了"})
        status, body, _ = self._get("/api/providers")
        profile = json.loads(body)["providers"][0]
        self.assertEqual(profile["label"], "改名了")
        self.assertTrue(profile["has_key"], "留空 key 时不能把已有的 key 清掉")

    def test_dream_model_endpoint_saves_and_clears(self) -> None:
        """整理模型（ADR-103）：保存、读回、清空（= 跟随默认）。"""
        status, result = self._post(
            "/api/providers/dream",
            {"provider": "deepseek", "model": "deepseek-v4-pro"},
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["dream"]["provider"], "deepseek")
        self.assertEqual(result["dream"]["model"], "deepseek-v4-pro")

        status, body, _ = self._get("/api/providers")
        state = json.loads(body)
        self.assertEqual(
            state["dream"],
            {"provider": "deepseek", "model": "deepseek-v4-pro", "effort": ""},
        )
        # 聊天默认一个字节都没动：两个选择是独立的
        self.assertEqual(state["default"]["model"], "deepseek-flash")

        status, result = self._post(
            "/api/providers/dream", {"provider": "", "model": ""}
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            result["dream"], {"provider": "", "model": "", "effort": ""}
        )

    def test_dream_model_endpoint_validates(self) -> None:
        """下拉框之外的写法一律拒绝：存进去的东西必须是能用的。"""
        # 只给接口、不给模型
        status, result = self._post(
            "/api/providers/dream", {"provider": "deepseek"}
        )
        self.assertEqual(status, 400)
        self.assertIn("模型", result["error"])

        # 只给模型、不给接口
        status, result = self._post(
            "/api/providers/dream", {"model": "deepseek-flash"}
        )
        self.assertEqual(status, 400)
        self.assertIn("接口", result["error"])

        # 没有这个接口
        status, result = self._post(
            "/api/providers/dream", {"provider": "nope", "model": "x"}
        )
        self.assertEqual(status, 404)

        # 接口里没有这个模型（界面上选不到，但手写请求能送到）
        status, result = self._post(
            "/api/providers/dream",
            {"provider": "deepseek", "model": "not-in-the-list"},
        )
        self.assertEqual(status, 400)
        self.assertIn("not-in-the-list", result["error"])

        # 三次拒绝都没有把它写进去
        status, body, _ = self._get("/api/providers")
        self.assertEqual(
            json.loads(body)["dream"],
            {"provider": "", "model": "", "effort": ""},
        )

    def test_dream_model_endpoint_rejects_a_provider_without_a_key(self) -> None:
        """没有 key 的接口不能拿来整理 —— 现在拒，别等整理跑起来才失败。"""
        status, _result = self._post(
            "/api/providers/save",
            {
                "id": "nokey",
                "preset": "__custom",
                "base_url": "https://nokey.example.com/v1",
                "models": "n-1",
            },
        )
        self.assertEqual(status, 200)
        status, result = self._post(
            "/api/providers/dream", {"provider": "nokey", "model": "n-1"}
        )
        self.assertEqual(status, 400)
        self.assertIn("API key", result["error"])

    def test_provider_save_rejects_empty_base_url(self) -> None:
        status, result = self._post(
            "/api/providers/save", {"id": "broken", "preset": "__custom", "base_url": ""}
        )
        self.assertEqual(status, 400)
        self.assertIn("base_url", result["error"])

    def test_provider_save_accepts_the_model_table(self) -> None:
        """模型表格提交的三样：id 列表 / 上限表 / 显示名表（ADR-092）。

        界面把"一行一个模型"的表格拆成这三个字段发上来，所以这里钉的是
        **三样一起生效**；顺带钉住"老的两串文本写法仍然能用"（老页面/别的客户端）。
        """
        status, result = self._post(
            "/api/providers/save",
            {
                "id": "deepseek",
                "models": ["deepseek-flash", "deepseek-v4-pro"],
                "model_limits": {"deepseek-v4-pro": 1_000_000},
                "model_labels": {
                    "deepseek-flash": "DeepSeek V4.1 Flash",
                    "deepseek-v4-pro": "   ",
                },
            },
        )
        self.assertEqual(status, 200, result)
        profile = next(p for p in result["state"]["providers"] if p["id"] == "deepseek")
        self.assertEqual(profile["models"], ["deepseek-flash", "deepseek-v4-pro"])
        self.assertEqual(profile["model_limits"], {"deepseek-v4-pro": 1_000_000})
        self.assertEqual(
            profile["model_labels"],
            {"deepseek-flash": "DeepSeek V4.1 Flash"},
            "空显示名等于没写（不是'名字叫空串'）",
        )

        # 下拉框：显示名字，值是 id
        labels = {item["model"]: item["label"] for item in result["state"]["models"]}
        self.assertEqual(
            labels["deepseek-flash"], f"{profile['label']} · DeepSeek V4.1 Flash"
        )
        self.assertEqual(labels["deepseek-v4-pro"], f"{profile['label']} · deepseek-v4-pro")

        # 老写法照旧：逗号串的模型 + 每行 model=上限
        status, result = self._post(
            "/api/providers/save",
            {"id": "deepseek", "models": "a, b", "model_limits": "a=8000\nb=999"},
        )
        self.assertEqual(status, 200, result)
        profile = next(p for p in result["state"]["providers"] if p["id"] == "deepseek")
        self.assertEqual(profile["models"], ["a", "b"])
        self.assertEqual(profile["model_limits"], {"a": 8000, "b": 999})

    def test_provider_save_accepts_unit_suffixes(self) -> None:
        """`200k` / `1M` 与 `200000` / `1000000` 是同一个数（ADR-093）。

        两种提交形状都要认：表格发的是字典，老写法是 `model=上限` 的多行文本。
        看不懂的值照旧按"没写"处理（档案层一贯的容错方向），但**界面那条路会拦住**
        —— 这里钉的是后端这一条。
        """
        status, result = self._post(
            "/api/providers/save",
            {
                "id": "deepseek",
                "models": ["deepseek-flash", "deepseek-v4-pro"],
                "model_limits": "deepseek-flash=200k\ndeepseek-v4-pro=1M",
            },
        )
        self.assertEqual(status, 200, result)
        profile = next(p for p in result["state"]["providers"] if p["id"] == "deepseek")
        self.assertEqual(
            profile["model_limits"],
            {"deepseek-flash": 200_000, "deepseek-v4-pro": 1_000_000},
        )

        # 字典形状（模型表格发的就是它）；`1.5M` 这种看不懂的仍然被跳过
        status, result = self._post(
            "/api/providers/save",
            {
                "id": "deepseek",
                "model_limits": {"deepseek-flash": "128K", "deepseek-v4-pro": "1.5M"},
            },
        )
        self.assertEqual(status, 200, result)
        profile = next(p for p in result["state"]["providers"] if p["id"] == "deepseek")
        self.assertEqual(profile["model_limits"], {"deepseek-flash": 128_000})

    def test_session_settings_round_trip(self) -> None:
        log = EventLog(self.settings.sessions_dir / "s-settings.jsonl", "s-settings")
        log.append("user_message", text="你好")
        log.close()

        status, result = self._post(
            "/api/sessions/s-settings/settings",
            {"provider": "deepseek", "model": "deepseek-v4-pro", "effort": "high"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])

        status, body, _ = self._get("/api/sessions/s-settings")
        generation = json.loads(body)["generation"]
        self.assertEqual(generation["model"], "deepseek-v4-pro")
        self.assertEqual(generation["effort"], "high")

    def test_session_settings_on_unknown_session(self) -> None:
        status, result = self._post(
            "/api/sessions/nope/settings", {"provider": "deepseek", "model": "m"}
        )
        self.assertEqual(status, 404)

    # -- 会话级联网搜索开关 -------------------------------------------

    def test_web_search_is_off_by_default(self) -> None:
        """新会话默认**不能**自己搜。"""
        log = EventLog(self.settings.sessions_dir / "s-tools.jsonl", "s-tools")
        log.append("user_message", text="你好")
        log.close()

        status, body, _ = self._get("/api/sessions/s-tools")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["tools"]["web_search"])

    def test_session_tools_toggle_round_trip(self) -> None:
        log = EventLog(self.settings.sessions_dir / "s-tools2.jsonl", "s-tools2")
        log.append("user_message", text="你好")
        log.close()

        status, result = self._post(
            "/api/sessions/s-tools2/tools", {"web_search": True}
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["web_search"])

        status, body, _ = self._get("/api/sessions/s-tools2")
        self.assertTrue(json.loads(body)["tools"]["web_search"])

        # 关掉也要能落盘（最后一条为准）
        self._post("/api/sessions/s-tools2/tools", {"web_search": False})
        status, body, _ = self._get("/api/sessions/s-tools2")
        self.assertFalse(json.loads(body)["tools"]["web_search"])

    def test_session_tools_on_unknown_session(self) -> None:
        status, _result = self._post("/api/sessions/nope/tools", {"web_search": True})
        self.assertEqual(status, 404)

    def test_chat_emits_the_tools_frame(self) -> None:
        """tools 帧里的 web_search 来自会话配置（ADR-114），不是请求字段。

        默认配置 = 全部启用 → 新会话帧里 web_search 为真；请求里那个旧
        `web_search` 字段已被忽略（配置是唯一真相来源）。
        """
        request = urllib.request.Request(
            self.base + "/api/chat",
            data=json.dumps({"text": "你好", "web_search": False}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as resp:
            body = resp.read().decode("utf-8")
        self.assertIn("event: tools", body)
        frame = [
            json.loads(line[6:]) for line in body.split("\n")
            if line.startswith("data: ") and '"web_search"' in line
        ][0]
        self.assertTrue(frame["web_search"])

    def test_chat_tools_frame_follows_session_config(self) -> None:
        """会话配置里没有 web_search → 帧里就是关（出生前写好的配置被原样采用）。"""
        from harness.tool_config import CHAT_TOOLS, ToolConfigStore

        enabled = [n for n in CHAT_TOOLS if n not in ("web_search", "web_open_url")]
        ToolConfigStore(self.settings.data_dir).save_session("s-cfgframe", enabled)
        request = urllib.request.Request(
            self.base + "/api/chat",
            data=json.dumps({"session_id": "s-cfgframe", "text": "你好"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as resp:
            body = resp.read().decode("utf-8")
        frame = [
            json.loads(line[6:]) for line in body.split("\n")
            if line.startswith("data: ") and '"web_search"' in line
        ][0]
        self.assertFalse(frame["web_search"])

    def test_errors_after_the_stream_started_become_a_notice_frame(self) -> None:
        """SSE 已经开始之后再抛异常，**不能再写一个 HTTP 500**。

        那一刻 `200 + text/event-stream` 的响应头已经在线上了，再写 500 就是
        "一个 HTTP 响应套在事件流正文里"，而前端只认 `event:`/`data:` 帧 ——
        于是它当这一轮正常结束：用户看到"回答被截断"，而真正的原因一个字也看不到。
        现在改成发一条 `notice` 帧（前端本来就会显示成一条错误提示）。
        """
        from harness.loop import AgentLoop

        def boom(loop_self, user_text, attachments=(), *, modality="image"):
            loop_self.emit("step", step=1)      # 先把 SSE 真的开起来
            raise RuntimeError("模拟流中途的崩溃")

        with mock.patch.object(AgentLoop, "run", boom):
            request = urllib.request.Request(
                self.base + "/api/chat",
                data=json.dumps({"text": "你好"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=60) as resp:
                self.assertEqual(resp.status, 200)
                body = resp.read().decode("utf-8")

        self.assertNotIn("HTTP/1.1 500", body, "不该再写第二个响应")
        self.assertIn("event: notice", body)
        notices = [
            json.loads(line[len("data: "):])
            for line in body.split("\n")
            if line.startswith("data: ") and '"level"' in line
        ]
        errors = [item for item in notices if item.get("level") == "error"]
        self.assertTrue(errors, "错误必须以 notice 帧出现，而不是静默消失")
        self.assertIn("RuntimeError", errors[-1]["text"])

    def test_provider_save_rejects_a_base_url_without_scheme(self) -> None:
        """`api.deepseek.com/v1` 这种写法会让 `urllib.Request()` 抛 ValueError。

        以前它既不是 ProviderError 也不是 OSError，于是一路变成 500 + 一句 Python
        原文；现在在**写入口**就 400，并告诉用户怎么写。
        """
        status, result = self._post(
            "/api/providers/save",
            {"id": "deepseek", "base_url": "api.deepseek.com/v1"},
        )
        self.assertEqual(status, 400, result)
        self.assertIn("http", result["error"])

    def test_tool_test_endpoint_rejects_a_bad_count(self) -> None:
        status, result = self._post("/api/tools/test", {"query": "x", "count": "abc"})
        self.assertEqual(status, 400, result)
        self.assertIn("error", result)

    def test_a_lone_surrogate_in_the_body_does_not_crash_the_turn(self) -> None:
        r"""`"\ud800"` 是合法 JSON（JS 的 `JSON.stringify` 就会生成它），
        但之后写事件日志要做 UTF-8 编码 —— 那里会抛 UnicodeEncodeError，
        一条消息就能把整轮打崩，而且连"用户说了什么"都存不下来。"""
        raw = json.dumps({"text": "坏\ud800消息"}).encode("utf-8")
        request = urllib.request.Request(
            self.base + "/api/chat",
            data=raw,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as resp:
            self.assertEqual(resp.status, 200)
            body = resp.read().decode("utf-8")

        session_id = [
            json.loads(line[len("data: "):])["session_id"]
            for line in body.split("\n")
            if line.startswith("data: ") and '"session_id"' in line
        ][0]
        text = (self.settings.sessions_dir / f"{session_id}.jsonl").read_text(
            encoding="utf-8"
        )
        self.assertIn("坏", text)
        self.assertIn("消息", text)
        self.assertNotIn("\ud800", text, "代理字符必须被替换掉，不能写进日志")

    def test_two_handles_on_the_same_session_never_share_a_seq(self) -> None:
        """聊天在流式写、界面同时在改名 —— 两个 EventLog 各写各的，seq 不能撞。

        seq 是整理游标**唯一**的定位依据：撞了就等于有一条记录被游标无声吞掉，
        以后再也不会进档案。
        """
        path = self.settings.sessions_dir / "s-seq.jsonl"
        first = EventLog(path, "s-seq")
        second = EventLog(path, "s-seq")
        try:
            first.append("user_message", text="a")
            second.append("session_title", title="t")
            first.append("assistant_message", message={"content": "b"})
        finally:
            first.close()
            second.close()
        seqs = [
            json.loads(line)["seq"]
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(seqs, [1, 2, 3])

    def test_config_reports_whether_search_is_usable(self) -> None:
        status, body, _ = self._get("/api/config")
        self.assertEqual(status, 200)
        config = json.loads(body)
        self.assertTrue(config["web_enabled"])
        self.assertTrue(config["web_search_available"])

    def test_chat_reports_the_generation_it_used(self) -> None:
        request = urllib.request.Request(
            self.base + "/api/chat",
            data=json.dumps(
                {"text": "你好", "model": "deepseek-v4-pro", "effort": "max"}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as resp:
            body = resp.read().decode("utf-8")
        self.assertIn("event: generation", body)
        generation = [
            json.loads(line[6:])
            for line in body.split("\n")
            if line.startswith("data: ") and '"provider"' in line
        ][0]
        self.assertEqual(generation["model"], "deepseek-v4-pro")
        self.assertEqual(generation["effort"], "max")

    def test_empty_session_is_not_listed(self) -> None:
        """只有 session_start 的半截会话（客户端首轮中途断开）不该出现在会话列表里。

        这是真实使用中发现的：侧栏多出一个"（空会话）"。
        """
        log = EventLog(self.settings.sessions_dir / "empty.jsonl", "empty")
        log.append("session_start", model="x")
        log.close()
        status, body, _ = self._get("/api/sessions")
        self.assertEqual(json.loads(body)["sessions"], [])

        # 但只要有用户消息，就必须列出来（哪怕回复被中断了）
        log = EventLog(self.settings.sessions_dir / "half.jsonl", "half")
        log.append("user_message", text="你好")
        log.close()
        status, body, _ = self._get("/api/sessions")
        sessions = json.loads(body)["sessions"]
        self.assertEqual([s["id"] for s in sessions], ["half"])

    # -- 恢复出厂设置（危险区）---------------------------------------

    def _make_reset_target(self) -> None:
        log = EventLog(self.settings.sessions_dir / "s-reset.jsonl", "s-reset")
        log.append("user_message", text="你好")
        log.close()
        memory = MemoryStore(instruction_path=self.settings.memory_instruction_path, cursor_db_path=self.settings.memory_db_path)
        memory.create("用户爱喝红茶")
        memory.close()

    def test_reset_preview_does_not_delete_anything(self) -> None:
        """预览是 GET，绝不能有副作用 —— 否则一个误点的链接就把数据抹了。"""
        self._make_reset_target()
        status, body, _ = self._get("/api/reset")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["sessions"], 1)

        status, body, _ = self._get("/api/sessions")
        self.assertEqual(len(json.loads(body)["sessions"]), 1, "预览不能真删")

    def test_reset_wipes_everything_including_the_api_keys(self) -> None:
        """恢复出厂设置要能连 API key 一起抹掉。

        做得到这一点，是因为 key **只存在 data/providers.json 里** ——
        没有任何"主 key"藏在 .env 里当出厂配置。本测试直接扫磁盘，
        确认那个 key 字符串在数据目录里再也找不到。
        """
        self._make_reset_target()
        key = "test-key"
        providers_path = self.settings.data_dir / "providers.json"
        self.assertIn(key, providers_path.read_text(encoding="utf-8"))

        status, result = self._post("/api/reset", {})
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(result["deleted"]["sessions"], 1)
        self.assertEqual(result["deleted"]["memories"], 1)
        self.assertEqual(result["deleted"]["providers"], 1)

        status, body, _ = self._get("/api/sessions")
        self.assertEqual(json.loads(body)["sessions"], [], "对话历史应当清空")
        status, body, _ = self._get("/api/memories")
        self.assertEqual(json.loads(body)["items"], [], "记忆条目应当清空")
        status, body, _ = self._get("/api/providers")
        self.assertEqual(
            json.loads(body)["providers"], [], "接口（连同 key）应当清空，且不会自动重建"
        )

        # 磁盘层面也确认一遍：key 不该藏在任何文件里
        blob = b""
        for path in self.settings.data_dir.rglob("*"):
            if path.is_file():
                blob += path.read_bytes()
        self.assertNotIn(key.encode(), blob, "重置后不该在任何文件里留下 key")

    # -- 重命名 / 删除会话 ---------------------------------------------

    def _make_session(self, name: str, text: str = "你好") -> Path:
        path = self.settings.sessions_dir / f"{name}.jsonl"
        log = EventLog(path, name)
        log.append("user_message", text=text)
        log.close()
        return path

    def _post_during_check_to_act_race(
        self, session_id: str, action: str, payload: dict[str, Any]
    ) -> tuple[int, dict]:
        """在"看过锁"与"真正操作"之间确定性插入一次 chat 拿锁。

        旧实现的 `locked()` 会先记下 False，再被屏障卡住；测试线程这时拿住真锁，
        模拟 chat 恰好开跑。新实现不走这个快照，而是在同一屏障之后非阻塞 acquire，
        因而必须得到 409。两种实现都由同一组事件驱动，不靠 sleep 或运气。
        """
        real_lock = threading.Lock()
        entered = threading.Event()
        resume = threading.Event()

        class RaceLock:
            def _pause(self) -> None:
                entered.set()
                if not resume.wait(5):
                    raise AssertionError("锁竞态屏障超时")

            def locked(self) -> bool:
                result = real_lock.locked()
                self._pause()
                return result

            def acquire(self, blocking: bool = True) -> bool:
                self._pause()
                return real_lock.acquire(blocking=blocking)

            def release(self) -> None:
                real_lock.release()

        self.server._locks[session_id] = RaceLock()
        responses: list[tuple[int, dict]] = []
        errors: list[BaseException] = []

        def request() -> None:
            try:
                responses.append(self._post(f"/api/sessions/{session_id}/{action}", payload))
            except BaseException as exc:  # 把线程里的失败带回断言线程
                errors.append(exc)

        worker = threading.Thread(target=request, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(5), "请求没有到达会话锁")
            self.assertTrue(real_lock.acquire(blocking=False), "测试没能模拟 chat 拿锁")
            resume.set()
            worker.join(10)
        finally:
            resume.set()
            worker.join(10)
            if real_lock.locked():
                real_lock.release()
        self.assertFalse(worker.is_alive(), "HTTP 请求没有在屏障之后结束")
        self.assertEqual(errors, [])
        self.assertEqual(len(responses), 1)
        return responses[0]

    def test_rename_session(self) -> None:
        path = self._make_session("s-rename", "原来第一句话")
        status, result = self._post("/api/sessions/s-rename/title", {"title": "新名字"})
        self.assertEqual(status, 200)
        self.assertEqual(result["title"], "新名字")

        status, body, _ = self._get("/api/sessions")
        titles = {s["id"]: s["title"] for s in json.loads(body)["sessions"]}
        self.assertEqual(titles["s-rename"], "新名字")

        status, body, _ = self._get("/api/sessions/s-rename")
        self.assertEqual(json.loads(body)["title"], "新名字")

    def test_rename_is_append_only_and_reversible(self) -> None:
        """重命名是追加一条事件：改动前的内容仍在，清空就退回自动命名。"""
        path = self._make_session("s-rename2", "自动取的名字")
        before = path.read_text(encoding="utf-8")
        self._post("/api/sessions/s-rename2/title", {"title": "手动名"})
        after = path.read_text(encoding="utf-8")
        self.assertTrue(after.startswith(before), "旧内容必须原样保留在文件里")

        self._post("/api/sessions/s-rename2/title", {"title": ""})
        status, body, _ = self._get("/api/sessions")
        titles = {s["id"]: s["title"] for s in json.loads(body)["sessions"]}
        self.assertEqual(titles["s-rename2"], "自动取的名字")

    def test_rename_unknown_session(self) -> None:
        status, _result = self._post("/api/sessions/nope/title", {"title": "x"})
        self.assertEqual(status, 404)

    def test_delete_session_leaves_everything_else_byte_identical(self) -> None:
        """用户的要求：**除了删掉指定的对话，没有任何其他影响**。

        所以这里比对字节：另一个会话、记忆库、接口配置都必须一个字节都没变。
        """
        doomed = self._make_session("s-doomed", "要被删掉的对话")
        keep = self._make_session("s-keep", "要保留的对话")

        memory = MemoryStore(instruction_path=self.settings.memory_instruction_path, cursor_db_path=self.settings.memory_db_path)
        memory.create("用户爱喝红茶")
        memory.close()
        providers_path = self.settings.data_dir / "providers.json"
        providers_path.write_text('{"providers": [], "default": {}}', encoding="utf-8")

        before = {
            "keep": keep.read_bytes(),
            "memory": self.settings.memory_db_path.read_bytes(),
            "providers": providers_path.read_bytes(),
        }

        status, result = self._post("/api/sessions/s-doomed/delete", {})
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])

        self.assertFalse(doomed.exists(), "指定的对话应当被删掉")
        self.assertEqual(keep.read_bytes(), before["keep"], "别的会话不能被动")
        self.assertEqual(
            self.settings.memory_db_path.read_bytes(), before["memory"],
            "记忆库不能被动 —— 记忆是独立于对话的长期资产",
        )
        self.assertEqual(
            providers_path.read_bytes(), before["providers"], "接口配置不能被动"
        )

    def test_delete_keeps_memories_created_in_that_session(self) -> None:
        """删对话 ≠ 忘掉那次对话里记住的事。

        这是最容易"顺手做错"的地方：删会话时去清理它创建的记忆。
        记忆是用户资产，想忘掉请去记忆页删。
        """
        self._make_session("s-mem", "记住我喜欢红茶")
        memory = MemoryStore(instruction_path=self.settings.memory_instruction_path, cursor_db_path=self.settings.memory_db_path)
        memory.create("用户喜欢红茶")
        memory.close()

        self._post("/api/sessions/s-mem/delete", {})

        memory = MemoryStore(instruction_path=self.settings.memory_instruction_path, cursor_db_path=self.settings.memory_db_path)
        try:
            items = memory.active_items()
        finally:
            memory.close()
        self.assertEqual(len(items), 1)
        self.assertIn("红茶", items[0].content)

    def test_delete_unknown_session(self) -> None:
        status, _result = self._post("/api/sessions/nope/delete", {})
        self.assertEqual(status, 404)

    def test_delete_refuses_while_the_session_is_generating(self) -> None:
        """正在生成的会话不能删：循环还握着那个文件句柄。"""
        self._make_session("s-busy")
        lock = self.server.session_lock("s-busy")
        lock.acquire()
        try:
            status, result = self._post("/api/sessions/s-busy/delete", {})
        finally:
            lock.release()
        self.assertEqual(status, 409)
        self.assertIn("生成中", result["error"])
        self.assertTrue((self.settings.sessions_dir / "s-busy.jsonl").exists())

    def test_delete_cannot_race_chat_between_lock_check_and_delete(self) -> None:
        path = self._make_session("s-delete-race")
        status, result = self._post_during_check_to_act_race(
            "s-delete-race", "delete", {}
        )
        self.assertEqual(status, 409, result)
        self.assertIn("生成中", result["error"])
        self.assertTrue(path.is_file(), "chat 已在检查之后拿到锁，就绝不能再删")

    def test_fork_cannot_race_chat_between_lock_check_and_copy(self) -> None:
        path = self._make_session("s-fork-race")
        before = set(self.settings.sessions_dir.glob("*.jsonl"))
        status, result = self._post_during_check_to_act_race(
            "s-fork-race", "fork", {"seq": 1}
        )
        self.assertEqual(status, 409, result)
        self.assertIn("生成中", result["error"])
        self.assertTrue(path.is_file())
        self.assertEqual(set(self.settings.sessions_dir.glob("*.jsonl")), before)

    def test_delete_and_fork_release_the_lock_before_every_response(self) -> None:
        """ADR-077：连 400 也不能在握着锁时回包，否则下一次操作会吃到假 409。"""
        self._make_session("s-delete-response")
        self._make_session("s-fork-response")
        delete_lock = self.server.session_lock("s-delete-response")
        fork_lock = self.server.session_lock("s-fork-response")
        observed: list[tuple[str, bool]] = []
        original = HarnessHandler._send_json

        def send_json(handler, payload, status=200):
            if handler.path == "/api/sessions/s-delete-response/delete":
                observed.append((handler.path, delete_lock.locked()))
            elif handler.path == "/api/sessions/s-fork-response/fork":
                observed.append((handler.path, fork_lock.locked()))
            return original(handler, payload, status)

        with mock.patch.object(HarnessHandler, "_send_json", new=send_json):
            delete_status, delete_result = self._post(
                "/api/sessions/s-delete-response/delete", {}
            )
            bad_status, _bad_result = self._post(
                "/api/sessions/s-fork-response/fork", {"seq": 0}
            )
            fork_status, fork_result = self._post(
                "/api/sessions/s-fork-response/fork", {"seq": 1}
            )

        self.assertEqual(delete_status, 200, delete_result)
        self.assertEqual(bad_status, 400)
        self.assertEqual(fork_status, 200, fork_result)
        self.assertTrue(fork_result["ok"])
        self.assertEqual(len(observed), 3)
        self.assertTrue(all(not locked for _path, locked in observed), observed)
        self.assertFalse(delete_lock.locked())
        self.assertFalse(fork_lock.locked())

    def test_chat_without_any_configured_provider_is_rejected(self) -> None:
        self.server.settings = test_settings(Path(self._tmp.name), api_key="")
        path = self.server.settings.data_dir / "providers.json"
        path.write_text('{"providers": [], "default": {}}', encoding="utf-8")
        status, result = self._post("/api/chat", {"text": "你好"})
        self.assertEqual(status, 400)
        self.assertIn("接口", result["error"])

    # -- 会话 id 校验：路径穿越（安全缺陷 1）--------------------------

    # 实测过的形状：`"..\\..\\victim"` 会让 `sessions_dir / f"{id}.jsonl"`
    # 落到数据目录外面，并在那里创建 / 追加 / 删除真实文件。
    BAD_SESSION_IDS = (
        "..\\..\\victim",
        "../../victim",
        "a/b",
        "a\\b",
        "..",
        ".",
        "sneaky.jsonl",
        "x" * 65,   # 超长
        "CON",      # Windows 设备名
        " nul ",
        "",
    )

    def _snapshot_tree(self, root: Path) -> dict[str, bytes]:
        """整棵树的"文件 → 内容"快照，用来证明"一个文件都没动"。"""
        if not root.exists():
            return {}
        return {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()}

    def _post_raw(
        self, path: str, body: bytes, headers: dict[str, str]
    ) -> tuple[int, dict]:
        """带自定义头的 POST（`_post` 只会发规范的 JSON 请求）。"""
        request = urllib.request.Request(
            self.base + path, data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return exc.code, {}

    def _sandbox_settings(self, name: str):
        """在一个沙箱里造 settings：data_dir = <沙箱>/data。

        这样 `sessions_dir/../../x` 正好落到 `<沙箱>/x`，能明确断言
        "数据目录外面的文件一个都没被碰过"。
        """
        sandbox = Path(self._tmp.name) / name
        settings = test_settings(sandbox / "data", request_timeout=1.0)
        seed_provider(settings, base_url="http://127.0.0.1:1")
        self.server.settings = settings
        return sandbox, settings

    def test_session_routes_reject_illegal_ids_and_touch_nothing(self) -> None:
        """所有 `/api/sessions/<id>/...` 都拒绝非法 id，且磁盘上不产生任何文件。

        id 来自 URL，直接拿去拼路径就是路径穿越 —— 读（GET 会话）、
        写（settings/tools/title 都是追加事件）、删（delete）四条路都要挡。
        """
        sandbox, _settings = self._sandbox_settings("sandbox-url")
        victim = sandbox / "victim.jsonl"   # = sessions_dir/../../victim.jsonl
        victim.write_text('{"kind":"user_message","text":"真实数据"}\n', encoding="utf-8")
        before = self._snapshot_tree(sandbox)

        for bad in self.BAD_SESSION_IDS:
            quoted = urllib.parse.quote(bad, safe="")
            status, _body, _ct = self._get(f"/api/sessions/{quoted}")
            self.assertEqual(status, 400, f"GET /api/sessions/{quoted!r} 应当 400")

            for action, payload in (
                ("settings", {"model": "m"}),
                ("tools", {"web_search": True}),
                ("title", {"title": "x"}),
                ("delete", {}),
            ):
                status, _result = self._post(f"/api/sessions/{quoted}/{action}", payload)
                self.assertEqual(
                    status, 400, f"POST /api/sessions/{quoted!r}/{action} 应当 400"
                )

        self.assertEqual(self._snapshot_tree(sandbox), before, "非法 id 不该动磁盘")
        self.assertEqual(victim.read_bytes(), before[str(victim)])

    def test_unencoded_slash_in_session_id_is_rejected(self) -> None:
        """`/api/sessions/a/b/delete`：`a/b` 必须被当成**一个**非法 id 看到。

        老的 `path.split("/")[3]` 会只取 `a`、把 `b` 当成动作名 ——
        非法 id 就这样被"截断成合法"了。所以这里特意不做百分号编码。
        """
        sandbox, _settings = self._sandbox_settings("sandbox-raw")
        before = self._snapshot_tree(sandbox)

        status, _body, _ct = self._get("/api/sessions/a/b")
        self.assertEqual(status, 400)
        for action in ("settings", "tools", "title", "delete"):
            status, _result = self._post(f"/api/sessions/a/b/{action}", {})
            self.assertEqual(status, 400, action)

        self.assertEqual(self._snapshot_tree(sandbox), before)

    def test_chat_rejects_a_traversing_session_id_and_writes_nothing(self) -> None:
        """实测复现过的那一条：`POST /api/chat` 的 body 里带 `..\\..\\victim`。

        修之前它会在数据目录外面**创建**一个 victim.jsonl，内容是真实事件 JSON；
        同类写法还能往任意 `*.jsonl` 追加。这里先把外面的文件造好，
        再确认既没被覆盖、也没被追加。
        """
        sandbox, _settings = self._sandbox_settings("sandbox-chat")
        victim = sandbox / "victim.jsonl"   # = sessions_dir/../../victim.jsonl
        original = '{"kind":"user_message","text":"别人的会话"}\n'
        victim.write_text(original, encoding="utf-8")
        before = self._snapshot_tree(sandbox)

        for bad in ("..\\..\\victim", "../../victim", "a/b", "victim.jsonl.bak"):
            status, result = self._post("/api/chat", {"session_id": bad, "text": "probe"})
            self.assertEqual(status, 400, f"{bad!r} 应当被拒绝：{result}")
            self.assertIn("非法", result["error"])

        self.assertEqual(victim.read_text(encoding="utf-8"), original, "不能写到外面的文件")
        self.assertEqual(self._snapshot_tree(sandbox), before, "非法 id 不该产生任何文件")

    def test_usage_endpoint_rejects_an_illegal_session_id(self) -> None:
        """只读接口也不能拼路径：`?session=..` 能把数据目录外面的日志读成用量。"""
        status, _body, _ct = self._get("/api/usage?session=..%5C..%5Cvictim")
        self.assertEqual(status, 400)

    def test_empty_session_id_still_starts_a_new_chat(self) -> None:
        """空 id 是**合法**输入：前端新开对话时送的就是 null/空串。

        这条是"别把校验做得太狠"的反向保险 —— 挡穿越不能顺手把正常功能挡掉。
        """
        request = urllib.request.Request(
            self.base + "/api/chat",
            data=json.dumps({"session_id": "", "text": "你好"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as resp:
            body = resp.read().decode("utf-8")
        self.assertIn("event: session", body)
        self.assertIn("event: done", body)

    # -- Host / DNS 重绑定 --------------------------------------------

    def test_loopback_host_gate_rejects_dns_rebinding_before_get_api(self) -> None:
        """恶意域名即使已经重绑定到 127.0.0.1，也不能读到本机 GET API。"""
        port = self.server.server_address[1]
        with mock.patch("harness.web.provider_store") as store:
            for host in (
                "evil.example",
                f"evil.example:{port}",
                f"127.0.0.1.evil.example:{port}",
            ):
                with self.subTest(host=host):
                    status, body, _ct = self._get("/api/config", {"Host": host})
                    self.assertEqual(status, 403)
                    self.assertIn("Host", json.loads(body)["error"])
            store.assert_not_called()

    def test_matching_malicious_host_and_origin_cannot_delete_a_session(self) -> None:
        """只比较 Origin == Host 不够：DNS 重绑定页能让这两个头一起叫 evil。"""
        path = self._make_session("s-rebinding")
        port = self.server.server_address[1]
        status, result = self._post_raw(
            "/api/sessions/s-rebinding/delete",
            b"{}",
            {
                "Content-Type": "application/json",
                "Host": f"evil.example:{port}",
                "Origin": f"http://evil.example:{port}",
            },
        )
        self.assertEqual(status, 403, result)
        self.assertIn("Host", result["error"])
        self.assertTrue(path.is_file(), "Host 闸必须在删除动作之前拒绝")

    def test_loopback_host_gate_accepts_ipv4_localhost_and_ipv6(self) -> None:
        port = self.server.server_address[1]
        for host in (
            f"127.0.0.1:{port}",
            f"127.0.0.2:{port}",
            f"localhost:{port}",
            f"LOCALHOST:{port}",
            f"localhost.:{port}",
            f"[::1]:{port}",
            f"[0:0:0:0:0:0:0:1]:{port}",
        ):
            with self.subTest(host=host):
                status, _body, _ct = self._get("/api/config", {"Host": host})
                self.assertEqual(status, 200)

    def test_loopback_host_gate_is_deterministic_about_ports_and_binding(self) -> None:
        from harness.web import _host_allowed_for_bind

        # 省略端口就是 HTTP 的 80；其它端口必须与实际监听端口完全一致。
        self.assertTrue(_host_allowed_for_bind("localhost", "127.0.0.1", 80))
        self.assertTrue(_host_allowed_for_bind("[::1]", "::1", 80))
        self.assertFalse(_host_allowed_for_bind("localhost", "127.0.0.1", 8787))
        self.assertFalse(_host_allowed_for_bind("localhost:8788", "127.0.0.1", 8787))
        for bad in (
            "",
            "evil.example:8787",
            "127.1:8787",          # 不做 inet_aton / DNS 的宽松解释
            "2130706433:8787",    # 十进制整数 IP 也不是合法 Host 字面量
            "::1:8787",           # IPv6 必须带方括号
            "[::1:8787",
            "[::1]:",
            "localhost:+8787",
            "localhost:0",
            "localhost:65536",
        ):
            with self.subTest(host=bad):
                self.assertFalse(_host_allowed_for_bind(bad, "127.0.0.1", 8787))

        # 既有 `--host 0.0.0.0` / 局域网托管不能被回环白名单顺手禁掉。
        self.assertTrue(_host_allowed_for_bind("evil.example", "0.0.0.0", 8787))
        self.assertTrue(_host_allowed_for_bind("host.example", "192.0.2.10", 8787))

    # -- 跨站 / CSRF（安全缺陷 2）-------------------------------------

    # 下面两条用例**刻意发空 body**。
    #
    # 原因是一个偶发的假失败（实测 25 次里挂 2~4 次）：
    # 服务端要在**读 body 之前**就按 Content-Type / Origin 拒绝（这正是防 CSRF 的
    # 正确顺序），于是请求体还躺在服务端的接收缓冲里，连接就被关掉了 ——
    # Windows 上这会变成一个 RST，客户端随后在 `http.client._read_status()` 里
    # 拿到 `ConnectionAbortedError [WinError 10053]`，连那个 400 都读不到。
    #
    # 这两条断言的是"**头**不对就拒绝"，与 body 内容无关，所以发空 body 既让
    # 竞态无从而生，又**不放宽任何断言**（400 + 什么都没被删掉，一个字都没改）。
    def test_reset_rejects_cross_site_posts(self) -> None:
        """跨站 POST /api/reset：任意网页就能抹掉全部会话、记忆与 API key。

        浏览器跨站发 POST 一定会带 `Origin`（`<form>` 也一样），
        新浏览器还带 `Sec-Fetch-Site` —— 这两者就是"这不是本站发起的"的凭据。
        """
        self._make_reset_target()
        session_path = self.settings.sessions_dir / "s-reset.jsonl"
        providers_path = self.settings.data_dir / "providers.json"
        key_before = providers_path.read_bytes()

        for headers in (
            {"Content-Type": "application/json", "Origin": "http://evil.example"},
            {"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"},
            {"Content-Type": "application/json", "Origin": "null"},
        ):
            status, result = self._post_raw("/api/reset", b"", headers)
            self.assertIn(status, (400, 403), f"{headers} 应当被拒绝")
            self.assertIn("跨站", result["error"])
            self.assertTrue(session_path.is_file(), "被拒绝的请求不能删掉任何数据")

        self.assertEqual(providers_path.read_bytes(), key_before, "API key 必须还在")

    def test_state_changing_posts_require_a_json_content_type(self) -> None:
        """表单能造出的 Content-Type 一律拒绝 —— 那正是浏览器"简单请求"的形状。"""
        self._make_reset_target()
        session_path = self.settings.sessions_dir / "s-reset.jsonl"

        for content_type in (
            "application/x-www-form-urlencoded",
            "multipart/form-data; boundary=x",
            "text/plain",
            "",
        ):
            headers = {"Content-Type": content_type} if content_type else {}
            status, _result = self._post_raw("/api/reset", b"", headers)
            self.assertEqual(status, 400, f"{content_type!r} 应当被拒绝")
            self.assertTrue(session_path.is_file(), "被拒绝的请求不能删掉任何数据")

    def test_same_origin_post_is_still_allowed(self) -> None:
        """同源 Origin（浏览器里的真实请求）必须照常放行，别把自己人挡在门外。"""
        self._make_reset_target()
        status, result = self._post_raw(
            "/api/reset",
            b"{}",
            {
                "Content-Type": "application/json",
                "Origin": self.base,
                "Sec-Fetch-Site": "same-origin",
            },
        )
        self.assertEqual(status, 200, result)
        self.assertTrue(result["ok"])

    # -- 危险 data_dir：Web 层要回 400 + 人话（安全缺陷 3）-------------

    def test_reset_refuses_when_data_dir_is_not_a_data_dir(self) -> None:
        """data_dir 指向"像项目目录的地方"时：400、人话、什么都不删。

        这里绝不拿真正的项目根来试 —— 万一守卫失效，那一下就是删源码。
        所以造一个"伪项目目录"（里面有 harness/ 和 .env）。
        """
        fake_project = Path(self._tmp.name) / "fake-project"
        (fake_project / "harness").mkdir(parents=True)
        (fake_project / ".env").write_text("DEEPSEEK_API_KEY=sk-real\n", encoding="utf-8")
        self.server.settings = test_settings(fake_project)

        status, result = self._post("/api/reset", {})
        self.assertEqual(status, 400)
        self.assertIn("拒绝", result["error"])
        self.assertTrue((fake_project / ".env").is_file(), "拒绝执行时一个文件都不能少")
        self.assertTrue((fake_project / "harness").is_dir())

    # -- 界面语言（ADR-085）------------------------------------------

    def test_language_endpoint_round_trip(self) -> None:
        """GET 给当前值 + 可选项；POST 落盘、且立刻反映到 /api/config。"""
        status, body, _ = self._get("/api/settings/language")
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertEqual(state["language"], "zh")
        self.assertEqual(state["languages"], ["zh", "en"])
        self.assertEqual(state["default"], "zh")

        status, result = self._post("/api/settings/language", {"language": "en"})
        self.assertEqual(status, 200, result)
        self.assertTrue(result["ok"])
        self.assertEqual(result["language"], "en")

        # 真的落盘了（路径由契约 §2 定死）
        saved = json.loads(
            (self.settings.data_dir / "settings.json").read_text(encoding="utf-8")
        )
        self.assertEqual(saved, {"language": "en"})

        # /api/config 立刻跟着变（前端启动时是拿它当初值的）
        _status, config, _ = self._get("/api/config")
        self.assertEqual(json.loads(config)["language"], "en")
        # 收尾：语言是进程级状态，别污染别的用例
        self.addCleanup(i18n.set_language, "zh")

    def test_language_rejects_unknown_value_and_writes_nothing(self) -> None:
        """不认识的取值要**明确报错**：`normalize()` 会把它折成默认值，
        那样"我写了 Japanese"会静默变成"切到了中文" —— 看起来成功、其实丢掉了用户的要求。"""
        status, result = self._post("/api/settings/language", {"language": "Japanese"})
        self.assertEqual(status, 400)
        self.assertIn("Japanese", result["error"])
        self.assertFalse(
            (self.settings.data_dir / "settings.json").exists(), "失败时不该落盘"
        )

    def test_language_accepts_aliases(self) -> None:
        for value, expected in (("en-US", "en"), ("中文", "zh"), ("English", "en")):
            with self.subTest(value=value):
                status, result = self._post("/api/settings/language", {"language": value})
                self.assertEqual(status, 200, result)
                self.assertEqual(result["language"], expected)
        self.addCleanup(i18n.set_language, "zh")

    def test_timezone_round_trip_and_reflects_in_config(self) -> None:
        """POST /api/settings/timezone 落盘；/api/config 立刻反映当前时区。"""
        status, body, _ = self._get("/api/config")
        self.assertEqual(json.loads(body)["timezone"], 8.0)

        status, result = self._post("/api/settings/timezone", {"timezone": -5})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["timezone"], -5.0)

        saved = json.loads(
            (self.settings.data_dir / "settings.json").read_text(encoding="utf-8")
        )
        self.assertEqual(saved["timezone"], -5.0)

        _status, config, _ = self._get("/api/config")
        self.assertEqual(json.loads(config)["timezone"], -5.0)

    def test_timezone_rejects_bad_value_and_writes_nothing(self) -> None:
        status, result = self._post("/api/settings/timezone", {"timezone": 99})
        self.assertEqual(status, 400)
        self.assertIn("99", result["error"])
        self.assertFalse(
            (self.settings.data_dir / "settings.json").exists(), "失败时不该落盘"
        )

    # -- 单轮最大输出（ADR-101）--------------------------------------

    def test_output_limit_round_trip_and_reflects_in_config(self) -> None:
        """接受 5k / -1；落盘；/api/config 立刻反映当前值。"""
        _status, body, _ = self._get("/api/config")
        self.assertEqual(
            json.loads(body)["max_output_tokens"], self.settings.max_output_tokens
        )

        status, result = self._post(
            "/api/settings/output-limit", {"max_output_tokens": "5k"}
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["max_output_tokens"], 5_000)

        saved = json.loads(
            (self.settings.data_dir / "settings.json").read_text(encoding="utf-8")
        )
        self.assertEqual(saved["max_output_tokens"], 5_000)

        _status, config, _ = self._get("/api/config")
        self.assertEqual(json.loads(config)["max_output_tokens"], 5_000)

        # -1 = 不限
        status, result = self._post(
            "/api/settings/output-limit", {"max_output_tokens": -1}
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["max_output_tokens"], -1)
        _status, config, _ = self._get("/api/config")
        self.assertEqual(json.loads(config)["max_output_tokens"], -1)

    def test_output_limit_rejects_bad_value_and_writes_nothing(self) -> None:
        for raw in (0, -2, "abc", "1.5M"):
            with self.subTest(raw=raw):
                status, result = self._post(
                    "/api/settings/output-limit", {"max_output_tokens": raw}
                )
                self.assertEqual(status, 400, result)
                self.assertIn("-1", result.get("error", ""))
                self.assertFalse(
                    (self.settings.data_dir / "settings.json").exists(),
                    "失败时不该落盘",
                )

    def test_malformed_json_is_reported_as_malformed(self) -> None:
        """坏 JSON 必须报"JSON 不合法"，**不能**报成"你没填这个字段"。

        实测踩过：用 PowerShell 写的 body 文件带 BOM，服务端报的是
        「要指定一个语言」—— 照那条报错去查，会一直查调用方为什么没传值，
        而真正的原因是 JSON 解析失败。症状指向错误的方向，比报错本身更贵。
        """
        for body in (b"\xef\xbb\xbf" + b'{"language":"en"}', b"{not json at all"):  # 带 BOM / 直接坏掉
            with self.subTest(body=body[:12]):
                status, result = self._post_raw(
                    "/api/settings/language", body,
                    {"Content-Type": "application/json"},
                )
                self.assertEqual(status, 400)
                self.assertIn("JSON", result.get("error", ""))
                self.assertNotIn("要指定一个语言", result.get("error", ""))

    def test_empty_body_is_not_reported_as_malformed(self) -> None:
        """没带 body 是"空请求"，与"坏 JSON"分开报（两件事，修的方向不一样）。"""
        status, result = self._post_raw(
            "/api/settings/language", b"", {"Content-Type": "application/json"}
        )
        self.assertEqual(status, 400)
        self.assertNotIn("JSON", result.get("error", ""))


class PortGuardTests(unittest.TestCase):
    """端口占用：**不许静默双绑**。

    背景（2026-10-16 真踩到）：`socketserver` 默认 `allow_reuse_address = True`，
    而这个标志在 Windows 上的语义是"允许抢占/重复绑定" —— 实测两个都设了它的
    harness 能**同时**绑住 8787，谁收到请求看运气。当天就有一个自检脚本打到了
    另一个实例上，两边的界面语言互相覆盖，看起来像"设置自己变了"。
    """

    def _bind(self, port: int):
        """占住一个端口（用普通 bind，与我们要探测的语义一致）。"""
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", port))
        sock.listen(5)
        self.addCleanup(sock.close)
        return sock

    def _free_port(self) -> int:
        """拿一个当前空闲的端口号（绑一下再放掉）。"""
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def test_port_is_taken_detects_a_listener(self) -> None:
        port = self._free_port()
        self.assertFalse(port_is_taken("127.0.0.1", port))
        self._bind(port)
        self.assertTrue(port_is_taken("127.0.0.1", port))

    def test_port_is_taken_ignores_the_ephemeral_zero(self) -> None:
        """"随便挑一个端口"永远不算被占用（否则测试全得靠固定端口）。"""
        self.assertFalse(port_is_taken("127.0.0.1", 0))

    def test_a_second_server_on_the_same_port_is_refused(self) -> None:
        """这才是根因本身：第二个实例必须**起不来**，而不是悄悄双绑。"""
        port = self._free_port()
        first = HarnessServer(("127.0.0.1", port), HarnessHandler)
        self.addCleanup(first.server_close)
        with self.assertRaises(OSError):
            HarnessServer(("127.0.0.1", port), HarnessHandler)

    def test_immediate_restart_on_the_same_port_still_works(self) -> None:
        """关掉再立刻重启必须成功 —— 否则这个加固会换来"重启要先等四分钟"。

        （实测：监听套接字关掉之后端口立刻可重绑，即使之前有连接进来过。
        这条断言就是为了防止以后有人把 `allow_reuse_address` 一刀切掉。）
        """
        port = self._free_port()
        first = HarnessServer(("127.0.0.1", port), HarnessHandler)
        first.server_close()
        second = HarnessServer(("127.0.0.1", port), HarnessHandler)
        second.server_close()

    def test_serve_refuses_a_taken_port_with_a_readable_message(self) -> None:
        """CLI 那条路上要给人话（并退出码 1），而不是一个 traceback。"""
        import contextlib
        import io
        import tempfile as _tempfile
        from pathlib import Path as _Path

        port = self._free_port()
        self._bind(port)
        with _tempfile.TemporaryDirectory() as tmp:
            settings = test_settings(_Path(tmp))
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = serve(settings, host="127.0.0.1", port=port, open_browser=False)
        self.assertEqual(code, 1)
        out = buffer.getvalue()
        self.assertIn(str(port), out)
        self.assertIn("不启动第二个", out)
        self.assertIn(f"--port {port + 1}", out, "要告诉用户下一步怎么做")


if __name__ == "__main__":
    unittest.main()
