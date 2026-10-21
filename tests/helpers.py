"""测试公共设施：临时配置、脚本化假 Provider、装配好的循环。

这些测试**永不访问网络** —— 模型后端被 FakeProvider 替换，
所以可以放心地反复运行，不花一分钱。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator, Sequence

from harness.config import Settings
from harness.events import EventLog
from harness.llm_types import (
    Finished,
    Message,
    ReasoningDelta,
    StreamEvent,
    TextDelta,
    ToolCallDelta,
    Usage,
)
from harness.loop import AgentLoop
from harness.memory import MemoryStore, open_memory
from harness.tools.memory_tools import MemCreateTool, MemDeleteTool, MemEditTool
from harness.tools.registry import ToolRegistry, ToolResult

_BASE = Settings(
    api_key="test-key",
    base_url="http://127.0.0.1:1",
    model="fake-model",
    reasoning_effort=None,
    request_timeout=5.0,
    max_output_tokens=256,
    max_steps=4,
    max_tokens_per_turn=100_000,
    max_wall_seconds=30.0,
    max_tool_failures=4,
    context_token_budget=8_000,
    show_reasoning=True,
    context_window=200_000,
    compact_enabled=True,
    compact_threshold=0.75,
    compact_target=0.35,
    compact_max_output_tokens=1024,
    compact_max_input_chars=40_000,
    memory_limit=50,
    max_memory_chars=100,
    max_tool_result_chars=2_000,
    memory_token_budget=6000,
    dream_max_sessions=3,
    dream_max_steps=30,
    dream_token_budget=150_000,
    dream_max_output_tokens=8192,
    dream_wall_seconds=600.0,
    dream_record_max_events=40,
    dream_record_max_chars=6000,
    vault_readme_token_budget=1200,
    vault_read_max_chars=4000,
    vault_write_max_chars=12000,
    vault_max_files=400,
    vault_name_max_chars=40,
    prompts_max_chars=20_000,
    data_dir=Path("."),
)


def test_settings(tmp_path: Path, **overrides: Any) -> Settings:
    return replace(_BASE, data_dir=Path(tmp_path), **overrides)


# --------------------------------------------------------------------------
# 脚本化假 Provider
# --------------------------------------------------------------------------


class FakeProvider:
    """按剧本逐次返回事件流，并记录每次收到的请求，便于断言"发出去的是什么"。"""

    name = "fake"

    def __init__(self, script: Sequence[Sequence[StreamEvent]] | None = None) -> None:
        self.script: list[Sequence[StreamEvent]] = [list(item) for item in (script or [])]
        self.calls: list[dict[str, Any]] = []

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]] = (),
        *,
        max_tokens: int | None = None,
    ) -> Iterator[StreamEvent]:
        self.calls.append(
            {
                "messages": list(messages),
                "tools": list(tools),
                "payloads": [message.to_api() for message in messages],
                "max_tokens": max_tokens,
            }
        )
        events = self.script.pop(0) if self.script else [Finished(reason="stop")]
        for event in events:
            yield event


def usage(total: int = 30, prompt: int = 20, completion: int = 10) -> Usage:
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
    )


def text_reply(text: str, reasoning: str = "", use: Usage | None = None) -> list[StreamEvent]:
    events: list[StreamEvent] = []
    if reasoning:
        events.append(ReasoningDelta(text=reasoning))
    events.append(TextDelta(text=text))
    events.append(Finished(reason="stop", usage=use or usage()))
    return events


def tool_reply(
    calls: Sequence[tuple[str, str, str]],
    text: str = "",
    use: Usage | None = None,
    reasoning: str = "",
) -> list[StreamEvent]:
    """calls: [(call_id, tool_name, arguments_json), ...]

    刻意把 arguments 拆成两片发出，用来验证流式拼装逻辑。
    """
    events: list[StreamEvent] = []
    if reasoning:
        events.append(ReasoningDelta(text=reasoning))
    if text:
        events.append(TextDelta(text=text))
    for index, (call_id, name, arguments) in enumerate(calls):
        events.append(ToolCallDelta(index=index, id=call_id, name=name))
        middle = max(1, len(arguments) // 2)
        events.append(ToolCallDelta(index=index, arguments_fragment=arguments[:middle]))
        events.append(ToolCallDelta(index=index, arguments_fragment=arguments[middle:]))
    events.append(Finished(reason="tool_calls", usage=use or usage()))
    return events


# --------------------------------------------------------------------------
# 装配
# --------------------------------------------------------------------------


class Harness:
    """测试用的最小 harness 装配体。"""

    def __init__(self, settings: Settings, provider: FakeProvider) -> None:
        self.settings = settings
        self.memory = open_memory(settings)
        self.registry = ToolRegistry(max_result_chars=settings.max_tool_result_chars)
        for tool in (
            MemCreateTool(self.memory),
            MemEditTool(self.memory),
            MemDeleteTool(self.memory),
        ):
            self.registry.register(tool)
        self.log = EventLog(settings.sessions_dir / "test-session.jsonl", "test-session")
        self.provider = provider
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.loop = AgentLoop(
            provider=provider,
            registry=self.registry,
            settings=settings,
            memory=self.memory,
            log=self.log,
            emit=self._emit,
        )

    def _emit(self, kind: str, **data: Any) -> None:
        self.events.append((kind, data))

    def close(self) -> None:
        self.log.close()
        self.memory.close()


def make_harness(
    tmp_path: Path, script: Sequence[Sequence[StreamEvent]], **overrides: Any
) -> Harness:
    settings = test_settings(tmp_path, **overrides)
    return Harness(settings, FakeProvider(script))


def seed_provider(
    settings: Settings,
    *,
    provider_id: str = "deepseek",
    api_key: str = "test-key",
    model: str = "deepseek-flash",
    base_url: str = "",
    effort: str = "",
) -> None:
    """给测试环境造一个"用户已经自己加过"的接口。

    本项目**不预置任何接口**（没有哪家是特殊的，key 也不来自 .env），
    所以需要接口的测试必须自己造一个 —— 这本身就是被测试的行为之一。
    """
    from harness.providers.store import (
        ProviderProfile,
        ProviderStore,
        get_preset,
    )

    preset = get_preset(provider_id)
    assert preset is not None, f"没有这个预设：{provider_id}"
    profile = ProviderProfile.from_preset(preset, api_key=api_key)
    if base_url:
        profile.base_url = base_url
    if model and model not in profile.models:
        profile.models.insert(0, model)

    store = ProviderStore(settings.data_dir / "providers.json")
    store.upsert(profile)
    store.set_defaults(provider=profile.id, model=model, effort=effort)
