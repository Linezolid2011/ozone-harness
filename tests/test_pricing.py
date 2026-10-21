"""计费功能已**永久删除**（M6 之后）。

为什么删掉而不是留着：单价表只能对一家成立。一旦支持多供应商，
"估算花费"就会拿 DeepSeek 的单价去算别家的用量 —— **一个错的数字比没有数字更糟**。
用量页现在改成"按接口分家"的 token 统计，逐对话统计保持原样。

官方价目仍值得知道（它解释了为什么这套 harness 花大力气安排消息顺序）：
输入·缓存命中比未命中便宜 **50 倍**（0.02 对 1 元），空闲时段是高峰的半价。
这些数字留在 `docs/06-getting-started.md` 里当背景资料，不再进代码。

这个文件保留成一个**守卫测试**：如果哪天有人把计费字段加回 Settings，
这里会红，提醒他先想清楚"多供应商下单价怎么算"。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.config import Settings

from .helpers import test_settings


class PricingRemovedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = test_settings(Path(self._tmp.name))

    def test_settings_has_no_pricing_fields(self) -> None:
        for field in (
            "price_input_per_million",
            "price_cached_input_per_million",
            "price_output_per_million",
            "price_offpeak_ratio",
        ):
            self.assertFalse(
                hasattr(self.settings, field),
                f"{field} 又回来了？多供应商下单价表无法通用，先想清楚再决定",
            )

    def test_settings_has_no_cost_api(self) -> None:
        for attribute in ("cost_of", "price_note", "is_peak", "prices_configured"):
            self.assertFalse(hasattr(self.settings, attribute), attribute)

    def test_env_pricing_keys_are_ignored(self) -> None:
        """就算 .env 里还留着旧的计费键，也不该影响任何行为。"""
        loaded = Settings.load()
        self.assertFalse(hasattr(loaded, "cost_of"))


if __name__ == "__main__":
    unittest.main()
