"""多供应商（M6）测试：档案持久化、key 掩码、模型发现、Settings 折算。

模型发现用**本机起一个假的 /models 服务**来测，不联网。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from harness.llm_types import Message
from harness.providers.base import ProviderError
from harness.providers.openai_compat import OpenAICompatProvider
from harness.providers.store import (
    PRESETS,
    ProviderProfile,
    ProviderStore,
    build_provider,
    fetch_models,
    get_preset,
    parse_token_limit,
    presets,
    resolve_settings,
)
from harness.runtime import (
    GenerationChoice,
    resolve_dream_provider,
    resolve_provider,
)

from .helpers import test_settings

DEEPSEEK = "https://api.deepseek.com"


class FakeModelsServer:
    """一个只会回 /models 的假供应商。"""

    last_headers: dict = {}
    last_path: str = ""

    class Handler(BaseHTTPRequestHandler):
        payload: dict = {"data": [{"id": "m-b"}, {"id": "m-a"}]}
        status = 200
        body_override: bytes | None = None

        def do_GET(self) -> None:  # noqa: N802
            FakeModelsServer.last_headers = dict(self.headers)
            FakeModelsServer.last_path = self.path
            payload = self.body_override or json.dumps(self.payload).encode("utf-8")
            self.send_response(self.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args) -> None:  # noqa: D102 - 测试里不需要访问日志
            return

    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.settings = test_settings(
            self.tmp, api_key="sk-from-env", model="deepseek-flash", reasoning_effort="low"
        )
        self.path = self.tmp / "providers.json"

    def store(self) -> ProviderStore:
        return ProviderStore(self.path)

    def seeded(self) -> ProviderStore:
        """造一份"用户已经自己加过"的接口档案。

        本项目**不预置任何接口**（去特殊化：DeepSeek 也只是普通一家），
        所以需要接口的测试要自己造，而不是指望 store 自动种一个。
        """
        store = self.store()
        if store.get("deepseek") is None:
            preset = get_preset("deepseek")
            assert preset is not None
            store.upsert(ProviderProfile.from_preset(preset, api_key="sk-from-env"))
        return store

    # -- 空状态与持久化 ----------------------------------------------

    def test_starts_with_no_providers_at_all(self) -> None:
        """全新机器上一个接口都没有 —— 没有任何一家是预置的。"""
        store = self.store()
        self.assertEqual(store.providers(), [])
        # defaults() 用空字符串而不是缺键，调用方就不用到处判 KeyError
        self.assertEqual(store.defaults(), {"provider": "", "model": "", "effort": ""})

    def test_reading_an_empty_store_does_not_create_the_file(self) -> None:
        """只是读一眼，不该顺手在磁盘上造出 providers.json。"""
        self.store()
        self.assertFalse(self.path.exists())

    # -- 整理（dream）专用的模型选择（ADR-103）-------------------------

    def test_dream_choice_starts_empty_and_follows_the_default(self) -> None:
        """默认「没设过」= 跟随全局默认，行为与加这个设置之前完全一致。"""
        store = self.store()
        self.assertEqual(
            store.dream_defaults(), {"provider": "", "model": "", "effort": ""}
        )

    def test_dream_choice_round_trips_and_clears(self) -> None:
        store = self.seeded()
        store.set_dream_defaults(
            provider="deepseek", model="deepseek-v4-pro", effort="high"
        )
        self.assertEqual(
            self.store().dream_defaults(),
            {"provider": "deepseek", "model": "deepseek-v4-pro", "effort": "high"},
        )
        # 空串 = 清掉 = 跟随默认（界面上那一项叫「跟随默认」）
        self.store().set_dream_defaults(provider="", model="", effort="")
        self.assertEqual(
            self.store().dream_defaults(), {"provider": "", "model": "", "effort": ""}
        )

    def test_removing_the_dream_provider_clears_the_choice(self) -> None:
        """删掉被选中的接口之后，整理不能继续指着一个不存在的 id。"""
        store = self.seeded()
        store.set_dream_defaults(provider="deepseek", model="deepseek-flash")
        store.remove("deepseek")
        self.assertEqual(
            self.store().dream_defaults(), {"provider": "", "model": "", "effort": ""}
        )

    def test_public_state_exposes_the_dream_choice(self) -> None:
        """前端靠 /api/providers 的 `dream` 画那个选择器（ADR-103）。"""
        store = self.seeded()
        store.set_dream_defaults(provider="deepseek", model="deepseek-v4-pro")
        state = self.store().public_state()
        self.assertEqual(
            state["dream"],
            {"provider": "deepseek", "model": "deepseek-v4-pro", "effort": ""},
        )
        # 与聊天默认是两个独立的键：改一个不该动另一个
        self.assertEqual(state["default"]["model"], "deepseek-flash")

    def test_round_trips_through_disk(self) -> None:
        store = self.seeded()
        store.update_key("deepseek", "sk-new")
        reopened = self.store()
        profile = reopened.get("deepseek")
        assert profile is not None
        self.assertEqual(profile.api_key, "sk-new")

    def test_adding_a_provider_keeps_the_base_url_you_gave_it(self) -> None:
        """回归测试：加接口时不能拿预设里的官方地址覆盖用户填的地址。

        早年的坑是"种默认档案时以预设地址为准"，后果是任何自定义端点
        （代理、自建网关、别的供应商）在首次运行后被**静默**改回官方地址。
        """
        store = self.store()
        profile = ProviderProfile(
            id="deepseek", label="DeepSeek", base_url="https://my-proxy.example.com/v1"
        )
        store.upsert(profile)
        reopened = self.store().get("deepseek")
        assert reopened is not None
        self.assertEqual(reopened.base_url, "https://my-proxy.example.com/v1")

    def test_corrupt_file_is_treated_as_empty(self) -> None:
        """文件坏了就当没有，而不是抛异常把整个界面卡死。"""
        self.path.write_text("{ this is not json", encoding="utf-8")
        store = self.store()
        self.assertEqual(store.providers(), [])

        # 坏文件也不该被悄悄"修复"成别的东西 —— 用户往里加接口时才覆盖它
        store.upsert(ProviderProfile(id="x", label="x", base_url="http://x"))
        self.assertEqual(len(self.store().providers()), 1)

    # -- key 安全 ----------------------------------------------------

    def test_public_state_never_exposes_the_key(self) -> None:
        store = self.seeded()
        blob = json.dumps(store.public_state(), ensure_ascii=False)
        self.assertNotIn("sk-from-env", blob)
        profile = store.get("deepseek")
        assert profile is not None
        self.assertIn("sk-fr", profile.masked_key())
        self.assertNotIn("sk-from-env", profile.masked_key())

    def test_short_key_is_fully_masked(self) -> None:
        profile = ProviderProfile(id="x", label="x", base_url="http://x", api_key="short")
        self.assertEqual(profile.masked_key(), "••••••")

    # -- 增删改 ------------------------------------------------------

    def test_upsert_update_remove(self) -> None:
        store = self.seeded()
        preset = get_preset("zhipu")
        assert preset is not None
        store.upsert(ProviderProfile.from_preset(preset, api_key="zk"))
        self.assertEqual(len(store.providers()), 2)

        store.update_models("zhipu", ["glm-x", "glm-y"])
        profile = store.get("zhipu")
        assert profile is not None
        self.assertEqual(profile.models, ["glm-x", "glm-y"])

        store.remove("zhipu")
        self.assertIsNone(store.get("zhipu"))
        self.assertEqual(len(store.providers()), 1)

    def test_removing_the_default_falls_back(self) -> None:
        store = self.seeded()
        preset = get_preset("ollama")
        assert preset is not None
        store.upsert(ProviderProfile.from_preset(preset, api_key="x"))
        store.set_defaults(provider="ollama", model="llama3")
        self.assertEqual(store.defaults()["provider"], "ollama")

        store.remove("ollama")
        self.assertEqual(store.defaults()["provider"], "deepseek")

    def test_all_models_skips_unconfigured_profiles(self) -> None:
        store = self.seeded()
        preset = get_preset("zhipu")
        assert preset is not None
        store.upsert(ProviderProfile.from_preset(preset))  # 没有 key
        models = store.all_models()
        self.assertTrue(all(entry["provider"] == "deepseek" for entry in models))
        self.assertGreaterEqual(len(models), 1)

    def test_id_is_sanitised(self) -> None:
        store = self.store()
        store.upsert(ProviderProfile(id="My API!!", label="x", base_url="http://x"))
        self.assertEqual(store.providers()[-1].id, "myapi")

    def test_upsert_rejects_empty_base_url(self) -> None:
        store = self.store()
        with self.assertRaises(ProviderError):
            store.upsert(ProviderProfile(id="broken", label="broken", base_url=""))

    def test_upsert_rejects_a_base_url_without_scheme(self) -> None:
        """`api.deepseek.com/v1` / `127.0.0.1:11434/v1` 是**人真的会这么打**的写法。

        但 `urllib.request.Request()` 在构造时就抛 `ValueError: unknown url type`，
        而那个异常既不是 ProviderError 也不是 OSError —— 它会一路穿到 HTTP 层变成
        500（界面上是一句 Python 原文），在对话路径上更是整轮直接中断。
        所以写入口就拒绝，并说清怎么写。
        """
        store = self.store()
        for bad in ("api.deepseek.com/v1", "127.0.0.1:11434/v1", "ftp://x/v1"):
            with self.subTest(bad=bad):
                with self.assertRaises(ProviderError) as ctx:
                    store.upsert(ProviderProfile(id="p", label="P", base_url=bad))
                self.assertIn("http", str(ctx.exception))

    def test_fetch_models_reports_a_bad_url_as_a_provider_error(self) -> None:
        """同一个坑的另一半：模型列表那条路也不能让 ValueError 漏出去。

        这条**不会联网**：`Request()` 在打开连接之前就抛了。
        """
        with self.assertRaises(ProviderError):
            fetch_models("api.deepseek.com/v1", "k")

    def test_save_is_atomic_and_leaves_no_partial_file(self) -> None:
        """保存走"临时文件 + os.replace"：不该在 data 里留下半截文件。

        `providers.json` 一旦写坏，读端会把它当成"没有配置" ——
        所有接口和 key 就这么"不见了"，而且没有任何提示。
        """
        store = self.store()
        store.upsert(
            ProviderProfile(id="p", label="P", base_url="https://x", api_key="k")
        )
        leftovers = [
            item.name for item in self.path.parent.iterdir() if ".tmp-" in item.name
        ]
        self.assertEqual(leftovers, [], "临时文件必须被换走或清掉")
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["providers"][0]["id"], "p")


class ResolveSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = test_settings(Path(self._tmp.name), api_key="env-key")

    def test_overrides_key_base_url_model(self) -> None:
        profile = ProviderProfile(
            id="p",
            label="P",
            base_url="https://p.example/v1",
            api_key="p-key",
            models=["p-model"],
            thinking_options=["low", "high"],
        )
        effective = resolve_settings(self.settings, profile, "", "high")
        self.assertEqual(effective.api_key, "p-key")
        self.assertEqual(effective.base_url, "https://p.example/v1")
        self.assertEqual(effective.model, "p-model")
        self.assertEqual(effective.reasoning_effort, "high")

    def test_no_thinking_options_means_do_not_send(self) -> None:
        """供应商没声明档位就不下发 —— 免得给它塞一个不认识的参数。"""
        profile = ProviderProfile(
            id="p", label="P", base_url="https://p.example/v1", api_key="k",
            models=["m"], thinking_options=[],
        )
        effective = resolve_settings(self.settings, profile, "", "high")
        self.assertIsNone(effective.reasoning_effort)

    def test_explicit_model_wins(self) -> None:
        profile = ProviderProfile(
            id="p", label="P", base_url="https://x/v1", api_key="k", models=["first", "second"]
        )
        self.assertEqual(resolve_settings(self.settings, profile, "second", "").model, "second")

    def test_budgets_are_not_touched(self) -> None:
        """换供应商不该影响预算与熔断配置。"""
        profile = ProviderProfile(id="p", label="P", base_url="https://x/v1", api_key="k")
        effective = resolve_settings(self.settings, profile, "", "")
        self.assertEqual(effective.max_steps, self.settings.max_steps)
        self.assertEqual(effective.max_tokens_per_turn, self.settings.max_tokens_per_turn)


class ResolveProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = test_settings(Path(self._tmp.name))

    def seed(self, profile_id: str = "deepseek", **kwargs) -> ProviderStore:
        store = ProviderStore(self.settings.data_dir / "providers.json")
        preset = get_preset(profile_id)
        assert preset is not None
        store.upsert(ProviderProfile.from_preset(preset, api_key=kwargs.pop("key", "k")))
        return store

    def test_uses_defaults_when_nothing_is_specified(self) -> None:
        store = self.seed()
        resolved = resolve_provider(self.settings, store=store)
        self.assertEqual(resolved.choice.provider_id, "deepseek")
        self.assertEqual(resolved.choice.model, "deepseek-flash")
        self.assertEqual(resolved.settings.api_key, "k")
        # 没设档位 → 不下发。
        # "没指定"就该是"不发送"，让供应商用它自己的默认值，而不是我们替它猜。
        self.assertEqual(resolved.choice.effort, "")

    def test_stored_effort_becomes_the_default_choice(self) -> None:
        store = self.seed()
        store.set_defaults(provider="deepseek", model="deepseek-flash", effort="low")
        resolved = resolve_provider(self.settings, store=store)
        self.assertEqual(resolved.choice.effort, "low")
        self.assertEqual(resolved.settings.reasoning_effort, "low")

    def test_explicit_values_win(self) -> None:
        store = self.seed("zhipu", key="zk")

        resolved = resolve_provider(
            self.settings, provider_id="zhipu", model="glm-4", store=store
        )
        self.assertEqual(resolved.choice.provider_id, "zhipu")
        self.assertEqual(resolved.choice.model, "glm-4")
        self.assertEqual(resolved.settings.api_key, "zk")
        # zhipu 预设没声明档位 → 不下发
        self.assertIsNone(resolved.settings.reasoning_effort)

    def test_no_providers_gives_a_readable_error(self) -> None:
        """一家都没有时必须是**看得懂**的提示，而不是一个空指针或 400。"""
        with self.assertRaises(ProviderError) as ctx:
            resolve_provider(self.settings)
        self.assertIn("还没有配置任何接口", str(ctx.exception))
        self.assertIn("设置 → 接口", str(ctx.exception))

    def test_provider_without_any_model_says_so(self) -> None:
        """加了一家却没模型时，别去拼一个空 model 发请求。"""
        store = ProviderStore(self.settings.data_dir / "providers.json")
        store.upsert(ProviderProfile(id="x", label="X", base_url="https://x/v1", api_key="k"))
        with self.assertRaises(ProviderError) as ctx:
            resolve_provider(self.settings, store=store)
        self.assertIn("还没有可用模型", str(ctx.exception))

    def test_unknown_provider_falls_back_to_first(self) -> None:
        store = self.seed()
        resolved = resolve_provider(
            self.settings, provider_id="does-not-exist", store=store
        )
        self.assertEqual(resolved.choice.provider_id, "deepseek")

    def test_generation_choice_round_trips(self) -> None:
        choice = GenerationChoice(provider_id="p", model="m", effort="low", label="P")
        self.assertEqual(GenerationChoice.from_record(choice.to_dict()), choice)


class ResolveDreamProviderTests(unittest.TestCase):
    """整理用哪个模型（ADR-103）：默认跟随全局默认，设了就用设的那一份。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = test_settings(Path(self._tmp.name))

    def seed_two(self) -> ProviderStore:
        """两家接口：deepseek（默认）+ 一家人造的小接口（有明确模型列表）。"""
        store = ProviderStore(self.settings.data_dir / "providers.json")
        preset = get_preset("deepseek")
        assert preset is not None
        store.upsert(ProviderProfile.from_preset(preset, api_key="dk"))
        store.upsert(
            ProviderProfile(
                id="x",
                label="X",
                base_url="https://x.example.com/v1",
                api_key="xk",
                models=["x-1", "x-2"],
            )
        )
        store.set_defaults(provider="deepseek", model="deepseek-flash")
        return store

    def test_unset_follows_the_global_default(self) -> None:
        store = self.seed_two()
        resolved = resolve_dream_provider(self.settings, store=store)
        self.assertEqual(resolved.choice.provider_id, "deepseek")
        self.assertEqual(resolved.choice.model, "deepseek-flash")

    def test_pinned_choice_wins(self) -> None:
        store = self.seed_two()
        store.set_dream_defaults(provider="x", model="x-2")
        resolved = resolve_dream_provider(self.settings, store=store)
        self.assertEqual(resolved.choice.provider_id, "x")
        self.assertEqual(resolved.choice.model, "x-2")
        self.assertEqual(resolved.settings.api_key, "xk")

    def test_pinned_choice_does_not_change_the_chat_default(self) -> None:
        """两个选择是独立的：改整理模型不该顺手把聊天模型也改了。"""
        store = self.seed_two()
        store.set_dream_defaults(provider="x", model="x-2")
        chat = resolve_provider(self.settings, store=store)
        self.assertEqual(chat.choice.provider_id, "deepseek")
        self.assertEqual(chat.choice.model, "deepseek-flash")

    def test_missing_provider_falls_back_to_the_global_default(self) -> None:
        """选过的那家被删了：退回全局默认，而不是悄悄用 profiles[0]。"""
        store = self.seed_two()
        store.set_dream_defaults(provider="x", model="x-2")
        store.remove("x")
        resolved = resolve_dream_provider(self.settings, store=store)
        self.assertEqual(resolved.choice.provider_id, "deepseek")
        self.assertEqual(resolved.choice.model, "deepseek-flash")

    def test_only_a_provider_uses_its_first_model(self) -> None:
        store = self.seed_two()
        store.set_dream_defaults(provider="x", model="")
        resolved = resolve_dream_provider(self.settings, store=store)
        self.assertEqual(resolved.choice.provider_id, "x")
        self.assertEqual(resolved.choice.model, "x-1")

    def test_removed_model_falls_back_to_the_global_default(self) -> None:
        """选的模型被从这家接口的列表里删掉：与界面一样退回「跟随默认」。"""
        store = self.seed_two()
        store.set_dream_defaults(provider="x", model="x-2")
        store.update_models("x", ["x-1"])  # x-2 被删了
        resolved = resolve_dream_provider(self.settings, store=store)
        self.assertEqual(resolved.choice.provider_id, "deepseek")
        self.assertEqual(resolved.choice.model, "deepseek-flash")

    def test_effort_is_passed_through(self) -> None:
        store = self.seed_two()
        store.set_dream_defaults(
            provider="deepseek", model="deepseek-v4-pro", effort="high"
        )
        resolved = resolve_dream_provider(self.settings, store=store)
        self.assertEqual(resolved.choice.effort, "high")
        self.assertEqual(resolved.settings.reasoning_effort, "high")

    def test_cli_only_effort_overrides_the_stored_one(self) -> None:
        """`--dream --effort high` 只改这一轮的档位，不动存着的模型 / 档位。"""
        store = self.seed_two()
        store.set_dream_defaults(
            provider="deepseek", model="deepseek-v4-pro", effort="low"
        )
        resolved = resolve_dream_provider(self.settings, store=store, effort="high")
        self.assertEqual(resolved.choice.provider_id, "deepseek")
        self.assertEqual(resolved.choice.model, "deepseek-v4-pro")
        self.assertEqual(resolved.choice.effort, "high")
        # 存储里的那份没被改写（重新从磁盘读一份来证明）
        self.assertEqual(
            ProviderStore(store.path).dream_defaults()["effort"], "low"
        )


class ReasoningContentFlagTests(unittest.TestCase):
    """回传思维链是**供应商相关**的：DeepSeek 要求，OpenAI 更严格。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = test_settings(Path(self._tmp.name), api_key="k")

    def _payload(self, send: bool) -> dict:
        provider = OpenAICompatProvider(
            self.settings, label="X", send_reasoning_content=send
        )
        from harness.llm_types import Message, ToolCall

        history = [
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall("c1", "mem_create", "{}")],
                reasoning="内部思维",
            )
        ]
        return provider.build_payload(history)

    def test_sent_when_the_vendor_requires_it(self) -> None:
        payload = self._payload(True)
        self.assertEqual(payload["messages"][0]["reasoning_content"], "内部思维")

    def test_stripped_for_stricter_vendors(self) -> None:
        payload = self._payload(False)
        self.assertNotIn("reasoning_content", payload["messages"][0])
        self.assertIn("tool_calls", payload["messages"][0])

    def test_the_deepseek_preset_builds_a_provider_that_sends_it(self) -> None:
        """DeepSeek 没有任何特殊类 —— 它只是**预设里把那个开关打开了**的那一家。"""
        preset = get_preset("deepseek")
        assert preset is not None
        profile = ProviderProfile.from_preset(preset, api_key="k")
        provider = build_provider(resolve_settings(self.settings, profile, "", ""), profile)

        payload = provider.build_payload(
            [Message(role="assistant", content="", reasoning="想")]
        )
        self.assertEqual(payload["messages"][0]["reasoning_content"], "想")

    def test_other_presets_build_a_provider_that_does_not(self) -> None:
        preset = get_preset("openai")
        assert preset is not None
        profile = ProviderProfile.from_preset(preset, api_key="k")
        provider = build_provider(resolve_settings(self.settings, profile, "", ""), profile)
        payload = provider.build_payload(
            [Message(role="assistant", content="", reasoning="想")]
        )
        self.assertNotIn("reasoning_content", payload["messages"][0])


class PresetTests(unittest.TestCase):
    def test_presets_are_well_formed(self) -> None:
        ids = [p.id for p in PRESETS]
        self.assertEqual(len(ids), len(set(ids)), "预设 id 不能重复")
        for preset in presets():
            self.assertTrue(preset["label"])
            self.assertTrue(preset["base_url"].startswith(("http://", "https://")))

    def test_only_deepseek_prefills_models(self) -> None:
        """刻意不预填各家的模型名 —— 我们不掌握它们在售的 id，写死只会误导。"""
        for preset in PRESETS:
            if preset.id == "deepseek":
                self.assertTrue(preset.models)
            else:
                self.assertEqual(preset.models, (), preset.id)

    def test_deepseek_requires_reasoning_content(self) -> None:
        preset = get_preset("deepseek")
        assert preset is not None
        self.assertTrue(preset.send_reasoning_content)
        self.assertIn("low", preset.thinking_options)


class ModelLabelTests(unittest.TestCase):
    """模型的**显示名**（ADR-092）：只影响界面，不参与任何决策。

    界面上它和"模型 id / 上下文上限"同处一张表（一行一个模型），所以最要紧的
    两条是：没填必须退回 id（不是空字符串），而**发出去的值永远是 id**。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "providers.json"

    def test_display_name_falls_back_to_the_id(self) -> None:
        profile = ProviderProfile(id="x", label="X", base_url="https://x", models=["m"])
        self.assertEqual(profile.display_name("m"), "m")
        self.assertEqual(profile.display_name("  m  "), "m", "两头空白不算名字的一部分")
        profile.model_labels = {"m": "  M 显示名 "}
        self.assertEqual(profile.display_name("m"), "M 显示名")
        self.assertEqual(profile.display_name("别的模型"), "别的模型")

    def test_empty_label_means_no_label(self) -> None:
        """空串等于没写：界面上那一格该退回 id，而不是变成空白。"""
        restored = ProviderProfile.from_dict({
            "id": "p", "label": "P", "base_url": "https://x",
            "models": ["a", "b"], "model_labels": {"a": "A", "b": "   "},
        })
        self.assertEqual(restored.model_labels, {"a": "A"})
        self.assertEqual(restored.display_name("b"), "b")

    def test_labels_round_trip_and_stay_out_of_the_decision_path(self) -> None:
        """显示名不改任何决策：上下文窗口照旧按模型 id 查。"""
        profile = ProviderProfile(
            id="p", label="P", base_url="https://x", api_key="k",
            models=["big"], model_limits={"big": 1_000_000},
            model_labels={"big": "Big Model"},
        )
        restored = ProviderProfile.from_dict(profile.to_storage())
        self.assertEqual(restored.model_labels, {"big": "Big Model"})
        self.assertEqual(restored.context_window("big"), 1_000_000)

    def test_all_models_shows_the_label_but_sends_the_id(self) -> None:
        """下拉框里显示名字，**选中的值必须是 id** —— 两件事分开。"""
        store = ProviderStore(self.path)
        store.upsert(ProviderProfile(
            id="p", label="P", base_url="https://x", api_key="k",
            models=["m1", "m2"], model_labels={"m1": "M One"},
        ))
        models = store.all_models()
        self.assertEqual([item["model"] for item in models], ["m1", "m2"])
        self.assertEqual([item["label"] for item in models], ["P · M One", "P · m2"])
        self.assertEqual(models[0]["model_label"], "M One")


class TokenLimitParsingTests(unittest.TestCase):
    """上下文上限的写法（ADR-093）：`1k` / `1K` = 1000，`1m` / `1M` = 1000k。

    这是用户真会这么打的输入（Kimi 的界面就允许），而它有两个方向都可能写错：
    把 k 当成 1024（那是 KiB），或者只认大写、只认小写。
    """

    def test_plain_numbers_pass_through(self) -> None:
        for raw, expected in (
            (1_000_000, 1_000_000),
            ("200000", 200_000),
            (" 65536 ", 65_536),
            ("200000\n", 200_000),
        ):
            self.assertEqual(parse_token_limit(raw), expected, repr(raw))

    def test_k_and_m_are_decimal_not_binary(self) -> None:
        cases = {
            "1k": 1_000, "1K": 1_000, "200k": 200_000, "200K": 200_000,
            "8k": 8_000, "128k": 128_000,
            "1m": 1_000_000, "1M": 1_000_000, "1000k": 1_000_000, "1000K": 1_000_000,
        }
        for raw, expected in cases.items():
            self.assertEqual(parse_token_limit(raw), expected, raw)
        # 这一条专门盯着"别把它当 1024"：1024 会让 200k 变成 204800。
        self.assertEqual(parse_token_limit("200k"), 200_000)
        self.assertNotEqual(parse_token_limit("200k"), 200 * 1024)

    def test_rejects_what_we_do_not_understand(self) -> None:
        """看不懂就返回 None —— 每个调用方自己决定要不要说出来。"""
        for raw in (
            "1.5M", "1.5k", "1kk", "1k5", "-1k", "k", "M", "", "  ", "1e6",
            "1kb", "1KiB", "1m tokens", "abc", None, True, False, 0, "0", "0k",
            "1,000,000", "200 k", "+100",
        ):
            self.assertIsNone(parse_token_limit(raw), repr(raw))

    def test_stored_limits_accept_the_documented_syntax(self) -> None:
        """手改 `providers.json` 写 `"1M"` 也该认（原来 `int()` 会把它静默丢掉）。"""
        profile = ProviderProfile.from_dict({
            "id": "p", "label": "P", "base_url": "https://x", "models": ["big", "small"],
            "model_limits": {"big": "1M", "small": "200k", "broken": "1.5M"},
        })
        self.assertEqual(
            profile.model_limits, {"big": 1_000_000, "small": 200_000},
            "看得懂的换算，看不懂的跳过（一条坏值不该让整个档案读不出来）",
        )
        self.assertEqual(profile.context_window("big"), 1_000_000)
        self.assertEqual(profile.context_window("small"), 200_000)
        self.assertEqual(profile.context_window("broken"), 200_000, "坏值退回默认")


class ContextWindowTests(unittest.TestCase):
    """上下文上限**只有按模型这一层**。

    原先还有一层"按供应商"的整家默认值，被取消了：同一家的模型窗口差别太大
    （Kimi 有 8k 也有 256k，DeepSeek 从 64k 到 1M 都有），
    一个整家共用的数字必然是错的 —— 而且错的那个方向会让人以为"配好了"。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "providers.json"

    def _write(self, payload: dict) -> None:
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_unset_means_200k(self) -> None:
        from harness.providers.store import DEFAULT_CONTEXT_WINDOW

        self.assertEqual(DEFAULT_CONTEXT_WINDOW, 200_000)
        profile = ProviderProfile(id="x", label="X", base_url="https://x", models=["m"])
        self.assertEqual(profile.context_window("m"), 200_000)
        self.assertEqual(profile.context_window("从来没听过的模型"), 200_000)

    def test_per_model_override_wins(self) -> None:
        profile = ProviderProfile(
            id="x", label="X", base_url="https://x",
            models=["small", "big"], model_limits={"big": 1_000_000},
        )
        self.assertEqual(profile.context_window("big"), 1_000_000)
        self.assertEqual(profile.context_window("small"), 200_000)

    def test_public_state_has_no_provider_level_limit(self) -> None:
        """接口层不能再吐出 context_limit —— 否则前端会渲染出一个早就没有意义的输入框。"""
        profile = ProviderProfile(id="x", label="X", base_url="https://x")
        self.assertNotIn("context_limit", profile.to_public())
        self.assertIn("model_limits", profile.to_public())

    def test_legacy_provider_limit_is_migrated_to_every_model(self) -> None:
        """旧数据的整家值要折算成按模型 —— **不能静默丢掉**。

        丢了的话，用户设的 1M 会悄悄掉回 200k，compact 提前好几倍触发，
        而界面上没有任何提示。改的是"配置放在哪一层"，不该改掉效果。
        """
        self._write({
            "providers": [{
                "id": "deepseek", "label": "DeepSeek", "base_url": "https://api.deepseek.com",
                "api_key": "sk-x", "models": ["deepseek-flash", "deepseek-v4-pro"],
                "context_limit": 1_000_000, "model_limits": {},
            }],
            "default": {"provider": "deepseek", "model": "deepseek-flash", "effort": ""},
        })
        profile = ProviderStore(self.path).get("deepseek")
        self.assertEqual(profile.model_limits, {
            "deepseek-flash": 1_000_000, "deepseek-v4-pro": 1_000_000,
        })
        self.assertEqual(profile.context_window("deepseek-flash"), 1_000_000)
        self.assertEqual(profile.context_window("deepseek-v4-pro"), 1_000_000)

    def test_migration_does_not_write_during_load(self) -> None:
        """加载是读操作，不该有副作用。"""
        self._write({
            "providers": [{
                "id": "p", "label": "P", "base_url": "https://x", "api_key": "k",
                "models": ["m"], "context_limit": 500_000,
            }],
            "default": {"provider": "p", "model": "m", "effort": ""},
        })
        before = self.path.read_bytes()
        ProviderStore(self.path).providers()
        self.assertEqual(self.path.read_bytes(), before, "加载不该改盘上的文件")

    def test_migration_lands_on_disk_in_the_new_format_on_next_save(self) -> None:
        self._write({
            "providers": [{
                "id": "p", "label": "P", "base_url": "https://x", "api_key": "k",
                "models": ["m"], "context_limit": 500_000,
            }],
            "default": {"provider": "p", "model": "m", "effort": ""},
        })
        store = ProviderStore(self.path)
        store.upsert(store.get("p"))          # 随便一次保存
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        item = raw["providers"][0]
        self.assertNotIn("context_limit", item, "旧字段该在保存时自然消失")
        self.assertEqual(item["model_limits"], {"m": 500_000})

    def test_migration_uses_default_model_when_the_list_is_empty(self) -> None:
        self._write({
            "providers": [{
                "id": "p", "label": "P", "base_url": "https://x", "api_key": "k",
                "models": [], "context_limit": 300_000,
            }],
            "default": {"provider": "p", "model": "the-model", "effort": ""},
        })
        self.assertEqual(
            ProviderStore(self.path).get("p").model_limits, {"the-model": 300_000}
        )

    def test_existing_model_limits_win_over_the_legacy_value(self) -> None:
        """更具体的那份（按模型）说话，旧的整家值让位。"""
        self._write({
            "providers": [{
                "id": "p", "label": "P", "base_url": "https://x", "api_key": "k",
                "models": ["a", "b"], "context_limit": 999_999,
                "model_limits": {"a": 8_000},
            }],
            "default": {"provider": "p", "model": "a", "effort": ""},
        })
        profile = ProviderStore(self.path).get("p")
        self.assertEqual(profile.context_window("a"), 8_000)
        self.assertEqual(profile.context_window("b"), 200_000)

    def test_legacy_value_with_no_model_at_all_is_dropped_quietly(self) -> None:
        """连模型都没有的档案，那个值本来也无处生效 —— 丢掉，但不能崩。"""
        self._write({
            "providers": [{
                "id": "p", "label": "P", "base_url": "https://x", "api_key": "k",
                "models": [], "context_limit": 123_456,
            }],
            "default": {"provider": "other", "model": "m", "effort": ""},
        })
        self.assertEqual(ProviderStore(self.path).get("p").model_limits, {})

    def test_resolved_settings_carry_the_model_window(self) -> None:
        """折算成 Settings 时用的必须是**这个模型**的上限。"""
        from harness.config import Settings

        base = Settings.load()
        profile = ProviderProfile(
            id="p", label="P", base_url="https://x", api_key="k",
            models=["big"], model_limits={"big": 1_000_000},
        )
        self.assertEqual(resolve_settings(base, profile).context_window, 1_000_000)
        plain = ProviderProfile(id="p", label="P", base_url="https://x", api_key="k",
                                models=["m"])
        self.assertEqual(resolve_settings(base, plain).context_window, 200_000)


class FetchModelsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = FakeModelsServer()
        self.addCleanup(self.server.stop)
        self.addCleanup(setattr, FakeModelsServer, "last_headers", {})

    def test_discovers_and_sorts(self) -> None:
        models = fetch_models(self.server.base_url, "k")
        self.assertEqual(models, ["m-a", "m-b"])

    def test_sends_both_auth_styles(self) -> None:
        """OpenAI 兼容端点认 Bearer，Anthropic 兼容端点认 x-api-key。

        多发一个用不上的头是无害的，少发一个就直接 401 ——
        而 Kimi Code 这个我们没法实测的端点恰好是后者。
        """
        fetch_models(self.server.base_url, "sk-test")
        headers = {k.lower(): v for k, v in FakeModelsServer.last_headers.items()}
        self.assertEqual(headers.get("authorization"), "Bearer sk-test")
        self.assertEqual(headers.get("x-api-key"), "sk-test")
        self.assertIn("anthropic-version", headers)

    def test_models_url_keeps_the_v1_in_the_base(self) -> None:
        """base_url 里的 /v1 必须保留，否则模型列表打到错地址。"""
        fetch_models(self.server.base_url, "k")
        self.assertEqual(FakeModelsServer.last_path, "/v1/models")

    def test_accepts_bare_list_shape(self) -> None:
        FakeModelsServer.Handler.payload = [{"id": "only"}]  # type: ignore[assignment]
        self.addCleanup(setattr, FakeModelsServer.Handler, "payload", {"data": [{"id": "m-b"}, {"id": "m-a"}]})
        self.assertEqual(fetch_models(self.server.base_url, "k"), ["only"])

    def test_http_error_is_reported(self) -> None:
        original_status, original_body = FakeModelsServer.Handler.status, FakeModelsServer.Handler.body_override
        FakeModelsServer.Handler.status = 401
        FakeModelsServer.Handler.body_override = b'{"error":{"message":"bad key"}}'
        self.addCleanup(setattr, FakeModelsServer.Handler, "status", original_status)
        self.addCleanup(setattr, FakeModelsServer.Handler, "body_override", original_body)
        with self.assertRaises(ProviderError) as ctx:
            fetch_models(self.server.base_url, "k")
        self.assertIn("401", str(ctx.exception))

    def test_unreachable_host_is_reported(self) -> None:
        with self.assertRaises(ProviderError):
            fetch_models("http://127.0.0.1:1/v1", "k", timeout=2.0)

    def test_empty_model_list_is_reported(self) -> None:
        original = FakeModelsServer.Handler.payload
        FakeModelsServer.Handler.payload = {"data": []}  # type: ignore[assignment]
        self.addCleanup(setattr, FakeModelsServer.Handler, "payload", original)
        with self.assertRaises(ProviderError):
            fetch_models(self.server.base_url, "k")


if __name__ == "__main__":
    unittest.main()
