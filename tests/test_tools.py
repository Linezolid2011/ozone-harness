"""工具层测试：参数容错、异常隔离、结果截断、记忆三件套的接线。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from harness.memory import MemoryStore
from harness.tools.memory_tools import MemCreateTool, MemDeleteTool, MemEditTool
from harness.tools.registry import ToolRegistry, ToolResult


class BoomTool:
    name = "boom"
    description = "总是抛异常，用于验证异常隔离"
    parameters = {"type": "object", "properties": {}}

    def run(self, args: dict[str, Any]) -> ToolResult:
        raise RuntimeError("内部炸了")


class HugeTool:
    name = "huge"
    description = "返回超大结果，用于验证截断"
    parameters = {"type": "object", "properties": {}}

    def run(self, args: dict[str, Any]) -> ToolResult:
        return ToolResult(ok=True, payload={"blob": "x" * 5000}, display="巨大结果")


class ToolRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.store = MemoryStore(self.tmp / "memory.db", max_items=20, max_chars=60)
        self.addCleanup(self.store.close)
        self.registry = ToolRegistry(max_result_chars=200)
        self.registry.register(MemCreateTool(self.store))
        self.registry.register(MemEditTool(self.store))
        self.registry.register(MemDeleteTool(self.store))

    # -- 注册表行为 --------------------------------------------------

    def test_specs_are_sorted_and_well_formed(self) -> None:
        specs = self.registry.specs()
        names = [spec["function"]["name"] for spec in specs]
        self.assertEqual(names, ["mem_create", "mem_del", "mem_edit"])
        self.assertEqual(names, sorted(names), "tools 数组必须稳定排序，否则每轮前缀都会变")
        for spec in specs:
            self.assertEqual(spec["type"], "function")
            self.assertTrue(spec["function"]["description"])
            self.assertEqual(spec["function"]["parameters"]["type"], "object")

    def test_unknown_tool_is_not_an_exception(self) -> None:
        result = self.registry.call("nope", "{}")
        self.assertFalse(result.ok)
        self.assertIn("未知工具", result.payload["error"])
        self.assertIn("mem_create", result.payload["available_tools"])

    def test_bad_arguments_are_reported_not_raised(self) -> None:
        broken = self.registry.call("mem_create", "{不是 JSON")
        self.assertFalse(broken.ok)
        self.assertIn("不是合法 JSON", broken.payload["error"])

        not_object = self.registry.call("mem_create", "[1, 2, 3]")
        self.assertFalse(not_object.ok)
        self.assertIn("必须是 JSON 对象", not_object.payload["error"])

    def test_empty_arguments_treated_as_empty_object(self) -> None:
        result = self.registry.call("mem_create", "")
        self.assertFalse(result.ok)  # content 缺失
        self.assertIn("不能为空", result.payload["error"])

    def test_tool_exception_is_contained(self) -> None:
        self.registry.register(BoomTool())
        result = self.registry.call("boom", "{}")
        self.assertFalse(result.ok)
        self.assertIn("RuntimeError", result.payload["error"])

    def test_long_result_is_truncated(self) -> None:
        self.registry.register(HugeTool())
        result = self.registry.call("huge", "{}")
        content = self.registry.render_content(result)
        self.assertLessEqual(len(content), 300)
        parsed = json.loads(content)
        self.assertTrue(parsed["truncated"])

    # -- 记忆工具接线 ------------------------------------------------

    def test_memory_tools_round_trip(self) -> None:
        created = self.registry.call(
            "mem_create", json.dumps({"content": "用户喜欢美式咖啡", "category": "偏好"})
        )
        self.assertTrue(created.ok)
        memory_id = created.payload["id"]
        self.assertIn("记住", created.display)

        edited = self.registry.call(
            "mem_edit", json.dumps({"id": memory_id, "content": "用户更喜欢拿铁"})
        )
        self.assertTrue(edited.ok)
        self.assertEqual(self.store.get(memory_id).content, "用户更喜欢拿铁")  # type: ignore[union-attr]

        deleted = self.registry.call("mem_del", json.dumps({"id": memory_id}))
        self.assertTrue(deleted.ok)
        self.assertEqual(self.store.stats()["active"], 0)

    def test_non_integer_id_is_rejected_politely(self) -> None:
        for name in ("mem_edit", "mem_del"):
            result = self.registry.call(name, json.dumps({"id": "abc"}))
            self.assertFalse(result.ok)
            self.assertIn("整数", result.payload["error"])


if __name__ == "__main__":
    unittest.main()
