"""单轮最大输出（「设置 → 常规」，ADR-101）。

三件事要钉住：
  1. 写法：`5000` / `5k` / `1M` / `-1`（不限）—— 与上下文上限共用**同一个**解析器；
  2. 存储：`data/settings.json` 里可改、默认 5000、坏值明确报错；
  3. 行为：`-1` 时 OpenAI 兼容请求**不发送 max_tokens**；窗口预算仍然留出
     一块输出预留，不会让输入把窗口占满。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.config import UNLIMITED_OUTPUT
from harness.i18n import DEFAULT_MAX_OUTPUT_TOKENS, SETTINGS_FILE, SettingsStore, t
from harness.llm_types import (
    Message,
    estimate_messages_tokens,
    estimate_tools_tokens,
)
from harness.providers.anthropic_compat import AnthropicCompatProvider
from harness.providers.openai_compat import OpenAICompatProvider
from harness.runtime import Runtime, resolve_provider
from harness.token_limit import UNLIMITED, parse_token_limit

from .helpers import (
    FakeProvider,
    make_harness,
    seed_provider,
    test_settings,
    text_reply,
)


class ParserTests(unittest.TestCase):
    def test_unlimited_is_opt_in(self) -> None:
        self.assertEqual(parse_token_limit(-1, allow_unlimited=True), -1)
        self.assertEqual(parse_token_limit("-1", allow_unlimited=True), -1)
        # 上下文上限那条路径不传 allow_unlimited：窗口没有"不限"这回事
        self.assertIsNone(parse_token_limit(-1))
        self.assertIsNone(parse_token_limit("-1"))
        self.assertEqual(UNLIMITED, UNLIMITED_OUTPUT)

    def test_units(self) -> None:
        for raw, expected in (
            ("5000", 5_000),
            ("5k", 5_000),
            ("5K", 5_000),
            ("1m", 1_000_000),
            ("1M", 1_000_000),
            (5_000, 5_000),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(
                    parse_token_limit(raw, allow_unlimited=True), expected
                )

    def test_invalid_values(self) -> None:
        for raw in (0, "0", -2, "-2", "-1k", "1.5M", "abc", "", True, None):
            with self.subTest(raw=raw):
                self.assertIsNone(
                    parse_token_limit(raw, allow_unlimited=True), repr(raw)
                )


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / SETTINGS_FILE

    def test_default_does_not_create_a_file(self) -> None:
        store = SettingsStore(self.path, output_default=5_000)
        self.assertEqual(store.max_output_tokens(), 5_000)
        self.assertEqual(store.max_output_tokens(), DEFAULT_MAX_OUTPUT_TOKENS)
        self.assertFalse(self.path.exists(), "只看一眼不该创建文件")

    def test_set_get_round_trip(self) -> None:
        store = SettingsStore(self.path, output_default=5_000)
        self.assertEqual(
            store.set_max_output_tokens("5k"),
            {"ok": True, "max_output_tokens": 5_000},
        )
        self.assertEqual(
            SettingsStore(self.path, output_default=5_000).max_output_tokens(), 5_000
        )
        self.assertTrue(self.path.is_file())

    def test_unlimited_round_trip(self) -> None:
        store = SettingsStore(self.path, output_default=5_000)
        self.assertEqual(
            store.set_max_output_tokens("-1")["max_output_tokens"], -1
        )
        self.assertEqual(
            SettingsStore(self.path, output_default=5_000).max_output_tokens(), -1
        )

    def test_bad_value_is_reported_and_writes_nothing(self) -> None:
        store = SettingsStore(self.path, output_default=5_000)
        result = store.set_max_output_tokens("0")
        self.assertFalse(result["ok"])
        self.assertIn("-1", result["error"])
        self.assertFalse(self.path.exists(), "失败时不该落盘")

    def test_bad_stored_value_falls_back_to_default(self) -> None:
        self.path.write_text('{"max_output_tokens": 0}', encoding="utf-8")
        self.assertEqual(
            SettingsStore(self.path, output_default=5_000).max_output_tokens(), 5_000
        )

    def test_public_state_carries_the_value_and_default(self) -> None:
        state = SettingsStore(self.path, output_default=5_000).public_state()
        self.assertEqual(state["max_output_tokens"], 5_000)
        self.assertEqual(state["max_output_tokens_default"], 5_000)


class WindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_unlimited_reserves_a_real_block(self) -> None:
        settings = test_settings(
            self.tmp, max_output_tokens=-1, context_window=32_000
        )
        self.assertGreaterEqual(settings.output_reserve(), 1_024)
        self.assertLessEqual(settings.output_reserve(), 8_192)
        self.assertEqual(
            settings.input_room(overhead_tokens=0),
            settings.context_window
            - settings.output_reserve()
            - settings.context_margin(),
        )

    def test_unlimited_turn_passes_minus_one_and_still_fits(self) -> None:
        harness = make_harness(self.tmp, [text_reply("好")], max_output_tokens=-1)
        self.addCleanup(harness.close)
        harness.loop.run("你好")

        call = harness.provider.calls[-1]
        self.assertEqual(call["max_tokens"], -1, "循环要把 -1 原样交给 provider")
        estimated = estimate_messages_tokens(call["messages"]) + estimate_tools_tokens(
            call["tools"]
        )
        self.assertLessEqual(
            estimated + harness.settings.output_reserve(),
            harness.settings.context_window,
            "不限输出也必须给输出留出预算里的那块预留",
        )

    def test_context_limit_notice_counts_the_real_output_reserve_when_unlimited(
        self,
    ) -> None:
        """`-1` 时 notice 里的 `needed` 要用**输出预留**，不能把 -1 当数字加。

        旧写法 `estimated + max_output_tokens` 在 -1 时得出 `estimated - 1` ——
        数字看起来正常，实际少报了整整一块输出预留。
        """
        harness = make_harness(
            self.tmp,
            [text_reply("不该被调用")],
            max_output_tokens=-1,
            context_window=2_000,
            context_token_budget=1_000_000,
        )
        self.addCleanup(harness.close)
        seen: dict[str, int] = {}
        original = harness.loop._fit_request

        def spy(estimated: int, output_tokens: int):
            seen["estimated"] = int(estimated)
            return original(estimated, output_tokens)

        harness.loop._fit_request = spy  # type: ignore[method-assign]
        outcome = harness.loop.run("长" * 50_000)

        self.assertEqual(harness.provider.calls, [], "装不下时不许发请求")
        self.assertEqual(outcome.stop_reason, "context_limit")
        expected = t(
            "ui.loop.context_limit",
            window=harness.settings.context_window,
            needed=seen["estimated"] + harness.settings.output_reserve(),
        )
        notices = [
            record["text"]
            for record in harness.log.read_events()
            if record["kind"] == "notice"
        ]
        self.assertIn(expected, notices)


class MinOutputTests(unittest.TestCase):
    """`_fit_request` 的最小输出阈值来自 `Settings.min_output_tokens`（config.py）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_the_default_is_64_and_it_is_used(self) -> None:
        # 窗口 4000 → 余量 200 → limit 3800。
        harness = make_harness(
            self.tmp, [], context_window=4_000, max_output_tokens=1_000
        )
        self.addCleanup(harness.close)
        self.assertEqual(harness.settings.min_output_tokens, 64)
        self.assertIsNone(
            harness.loop._fit_request(3_750, 1_000), "只剩 50 token < 64 → 本地中止"
        )
        self.assertEqual(
            harness.loop._fit_request(3_700, 1_000),
            (100, True),
            "还剩 100 token → 发，但把输出上限压到 100",
        )

    def test_the_minimum_is_configurable(self) -> None:
        harness = make_harness(
            self.tmp,
            [],
            context_window=4_000,
            max_output_tokens=1_000,
            min_output_tokens=32,
        )
        self.addCleanup(harness.close)
        self.assertEqual(harness.settings.min_output_tokens, 32)
        self.assertEqual(
            harness.loop._fit_request(3_750, 1_000),
            (50, True),
            "50 token >= 配置的 32 → 应该发出去（压到 50）",
        )


class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = test_settings(Path(self._tmp.name))

    def test_runtime_applies_the_stored_value(self) -> None:
        store = SettingsStore(
            self.settings.data_dir / SETTINGS_FILE,
            output_default=self.settings.max_output_tokens,
        )
        store.set_max_output_tokens("1234")
        runtime = Runtime(self.settings, None, provider=FakeProvider([]))
        try:
            self.assertEqual(runtime.settings.max_output_tokens, 1_234)
            self.assertEqual(runtime.loop.settings.max_output_tokens, 1_234)
        finally:
            runtime.close()


class ResolveProviderTests(unittest.TestCase):
    """实测发现的那条：`-1` 的 Anthropic 兜底必须用**折算后**的 settings。

    旧路径里 provider 是在读 `data/settings.json` 之前造的，于是 `-1` 时它的
    `output_reserve()` 会读到 `.env` 的种子值（测试里是 256 / 生产是 5000），
    而循环的窗口预算用的是新值 —— 请求会比预算多带一截输出预留。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = test_settings(Path(self._tmp.name))

    def test_unlimited_is_applied_before_the_provider_is_built(self) -> None:
        from harness.providers.store import ProviderStore

        seed_provider(
            self.settings,
            provider_id="kimi-code",
            model="kimi-for-coding",
            base_url="http://127.0.0.1:1",
        )
        store = ProviderStore(self.settings.data_dir / "providers.json")
        profile = store.get("kimi-code")
        assert profile is not None
        profile.model_limits["kimi-for-coding"] = 12_000
        store.upsert(profile)
        (self.settings.data_dir / SETTINGS_FILE).write_text(
            '{"max_output_tokens": -1}', encoding="utf-8"
        )

        resolved = resolve_provider(
            self.settings, provider_id="kimi-code", model="kimi-for-coding"
        )
        self.assertEqual(resolved.settings.context_window, 12_000)
        self.assertEqual(resolved.settings.max_output_tokens, -1)
        payload = resolved.provider.build_payload(
            [Message(role="user", content="hi")]
        )
        # Anthropic 必填 max_tokens；-1 时应当用折算后窗口的输出预留（12000//4）。
        self.assertEqual(payload["max_tokens"], 3_000)
        self.assertEqual(payload["max_tokens"], resolved.settings.output_reserve())


class ProviderPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_openai_omits_max_tokens_when_unlimited(self) -> None:
        settings = test_settings(self.tmp, max_output_tokens=-1)
        provider = OpenAICompatProvider(settings, label="测试")
        payload = provider.build_payload([Message(role="user", content="hi")])
        self.assertNotIn("max_tokens", payload, "-1 不能当成一个数字发出去")

    def test_openai_sends_the_number_normally(self) -> None:
        settings = test_settings(self.tmp, max_output_tokens=5_000)
        provider = OpenAICompatProvider(settings, label="测试")
        payload = provider.build_payload([Message(role="user", content="hi")])
        self.assertEqual(payload["max_tokens"], 5_000)

    def test_anthropic_uses_the_reserve_when_unlimited(self) -> None:
        """Anthropic 的 max_tokens 是必填，不能省略；用预算里的输出预留兜底。"""
        settings = test_settings(self.tmp, max_output_tokens=-1)
        provider = AnthropicCompatProvider(settings, label="测试")
        payload = provider.build_payload([Message(role="user", content="hi")])
        self.assertEqual(payload["max_tokens"], settings.output_reserve())
        self.assertGreater(payload["max_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
