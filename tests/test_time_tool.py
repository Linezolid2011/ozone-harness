"""time 工具测试：只读、返回 UTC+8 的当前时间。"""
from __future__ import annotations

import re
import unittest

from harness.tools.time_tool import TimeTool


class TimeToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = TimeTool()

    def test_returns_utc_plus_8_time(self) -> None:
        result = self.tool.run({})
        self.assertTrue(result.ok)
        payload = result.payload
        self.assertEqual(payload["timezone"], "UTC+8")
        self.assertEqual(payload["offset"], "+08:00")
        self.assertRegex(payload["iso"], re.compile(r"[+-]\d{2}:\d{2}$"))
        self.assertRegex(
            payload["now"],
            re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"),
        )
        self.assertGreater(int(payload["unix"]), 0)
        self.assertTrue(payload["weekday"], "weekday 不该是空串")

    def test_has_no_parameters(self) -> None:
        self.assertEqual(
            self.tool.parameters,
            {"type": "object", "properties": {}, "required": []},
        )

    def test_description_and_display_are_present(self) -> None:
        self.assertTrue(self.tool.description.strip())
        display = self.tool.run({}).display
        self.assertIn("+08:00", display)

    def test_custom_offset_is_used(self) -> None:
        tool = TimeTool(offset_hours=-5)
        payload = tool.run({}).payload
        self.assertEqual(payload["timezone"], "UTC-5")
        self.assertEqual(payload["offset"], "-05:00")


if __name__ == "__main__":
    unittest.main()
