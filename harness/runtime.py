"""运行时装配：把 provider / 工具 / 记忆 / 会话日志拼成一个可用的 harness。

CLI 与 Web 端**共用这一份装配**。两个前端各自只负责把
`emit(kind, **data)` 事件渲染成人能看的东西（终端文字 / SSE 帧）。

M6 起多了"选哪家接口、哪个模型、哪个思考档位"这件事。
`resolve_provider()` 是唯一的决策点：它读 `data/providers.json`，
把选中的档案折算成一份 Settings，再据此造出 provider。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from .config import Settings
from .context import vault_prompt_text, web_tools_text
from .events import EventLog, new_session_id
from .i18n import max_output_tokens_for, t, timezone_for
from .loop import AgentLoop
from .memory import open_memory
from .net.config import TOOLS_FILE, ToolStore
from .providers.base import LLMProvider, ProviderError
from .providers.openai_compat import OpenAICompatProvider
from .providers.store import (
    ProviderProfile,
    ProviderStore,
    build_provider,
    resolve_settings,
)
from .tools.memory_tools import MemCreateTool, MemDeleteTool, MemEditTool
from .tools.registry import ToolRegistry
from .tools.time_tool import TimeTool
from .tools.vault_tools import VaultListTool, VaultReadTool, VaultSearchTool
from .tools.web_tools import WebOpenUrlTool, WebSearchTool
from .vault import VaultStore

PROVIDERS_FILE = "providers.json"

# 一个接口都没有时的提示。刻意指出"没有任何一家是预置的" ——
# 这是产品行为，不是错误信息，所以写得具体一点。
#
# 文案**现取**（见 `no_provider_help()`）：语言是运行期可切的。
# `cli.py` 从模块顶层 `import NO_PROVIDER_HELP`（命令行用户也得知道该怎么做），
# 所以那个旧名字用模块级 `__getattr__`（PEP 562）保留下来 ——
# 写成模块常量就等于在 import 那一刻把语言钉死了。
def no_provider_help() -> str:
    """一个接口都没配置时给的那句话（**现取**当前语言）。"""
    return t("ui.runner.no_provider")


def __getattr__(name: str) -> Any:
    if name == "NO_PROVIDER_HELP":
        return no_provider_help()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@dataclass
class GenerationChoice:
    """这一次生成用的是哪家、哪个模型、哪个档位。会写进会话日志。"""

    provider_id: str = ""
    model: str = ""
    effort: str = ""
    label: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider_id,
            "model": self.model,
            "effort": self.effort,
            "label": self.label,
        }

    @classmethod
    def from_record(cls, record: dict) -> "GenerationChoice":
        record = record or {}
        return cls(
            provider_id=str(record.get("provider") or ""),
            model=str(record.get("model") or ""),
            effort=str(record.get("effort") or ""),
            label=str(record.get("label") or ""),
        )


@dataclass
class ResolvedProvider:
    choice: GenerationChoice
    profile: ProviderProfile
    settings: Settings          # 已折算：provider 从这份里读 key / base_url / model
    provider: LLMProvider


def provider_store(settings: Settings) -> ProviderStore:
    return ProviderStore(settings.data_dir / PROVIDERS_FILE)


def resolve_provider(
    settings: Settings,
    *,
    provider_id: str = "",
    model: str = "",
    effort: str = "",
    store: ProviderStore | None = None,
) -> ResolvedProvider:
    """决定这一次生成用哪家、哪个模型、哪个档位。

    取值优先级：显式传入 > providers.json 里的默认 > 档案里的第一个模型。

    一个接口都没有时抛 ProviderError —— **没有任何一家是预置的**，
    所以第一件事永远是去界面上加一个（或让命令行用户看到该怎么做）。
    """
    store = store or provider_store(settings)
    default = store.defaults()

    wanted = (provider_id or default["provider"]).strip()
    profile = store.get(wanted) if wanted else None
    if profile is None:
        profiles = store.providers()
        if not profiles:
            raise ProviderError(no_provider_help())
        profile = profiles[0]

    is_default = profile.id == default["provider"]
    chosen_model = (
        (model or "").strip()
        or (default["model"] if is_default else "")
        or (profile.models[0] if profile.models else "")
    )
    if not chosen_model:
        raise ProviderError(t("ui.runner.no_model", label=profile.label))
    chosen_effort = (effort or "").strip()
    if not chosen_effort and is_default:
        chosen_effort = default["effort"]

    # 单轮最大输出是**运行期设置**（ADR-101）：必须在**造 provider 之前**折算进去。
    # 否则 `-1`（不限）时 Anthropic 的兜底 `max_tokens` 会读到 `.env` 的旧种子值，
    # 而循环的窗口预算用的是 `settings.json` 里的新值 —— 两边分家（实测发现：
    # 种子 5000 / 真正的预留 3000，请求会比预算多带 2000 token 的输出预留）。
    runtime_settings = replace(
        settings,
        max_output_tokens=max_output_tokens_for(
            settings.data_dir, settings.max_output_tokens
        ),
    )
    effective = resolve_settings(runtime_settings, profile, chosen_model, chosen_effort)
    provider = build_provider(effective, profile)
    return ResolvedProvider(
        choice=GenerationChoice(
            provider_id=profile.id,
            model=effective.model,
            effort=effective.reasoning_effort or "",
            label=profile.label,
        ),
        profile=profile,
        settings=effective,
        provider=provider,
    )


def resolve_dream_provider(
    settings: Settings,
    *,
    store: ProviderStore | None = None,
    effort: str = "",
) -> ResolvedProvider:
    """决定**整理（dream）**用哪家 / 哪个模型（ADR-103）。

    优先级：`providers.json` 里的 `dream` 选择 > 全局 `default`。

    * **没设过**（`dream.provider` 与 `dream.model` 都为空）→ 与加这个设置之前
      完全一样：走 `resolve_provider()` 的全局默认。这样全新机器不需要先配一遍，
      已有用户也不会因为升级而换模型；
    * **设了但接口已经不存在** → 同样退回全局默认。不能走 `resolve_provider()`
      内部那条"取 `profiles[0]`"的兜底：那会让它悄悄用**另一家**接口，
      而用户看到的是"我明明选过"（与本项目讨厌的静默降级同一条理由）；
    * **设了但模型已从这家接口的列表里删掉** → 也退回全局默认。界面那边
      同样显示成「跟随默认」（值不在列表里），两边必须一致；
    * **只设了 provider、没设 model** → 用这家档案里的第一个模型（与接口页
      "默认模型"那一栏的行为一致）。

    CLI 与 Web **都走这里**，所以"用哪个模型整理"只有一个答案。

    `effort` 只给命令行留一个口子：`--dream --effort high` 想只改这一轮的档位、
    不改模型时用（空串 = 用设置里存的那个）。
    """
    store = store or provider_store(settings)
    dream = store.dream_defaults()
    provider_id = dream["provider"].strip()
    model = dream["model"].strip()
    chosen_effort = (effort or dream["effort"]).strip()
    if not provider_id and not model:
        return resolve_provider(settings, effort=chosen_effort, store=store)
    profile = store.get(provider_id) if provider_id else None
    if profile is None:
        # 选过的那家已经被删了 / 档案被手改坏了：退回全局默认，**不改写配置**
        # （下一次用户在界面上显式选一次就会覆盖它）。
        return resolve_provider(settings, effort=chosen_effort, store=store)
    if model and profile.models and model not in profile.models:
        # 选过的模型已经从这家接口的模型列表里删掉了。界面那边会显示成
        # 「跟随默认」（值不在列表里），所以这里也必须退回全局默认 ——
        # 否则界面说"跟随默认"、实际却还在用那个已删掉的模型。
        return resolve_provider(settings, effort=chosen_effort, store=store)
    if not model and profile.models:
        model = profile.models[0]
    return resolve_provider(
        settings,
        provider_id=profile.id,
        model=model,
        effort=chosen_effort,
        store=store,
    )


class Runtime:
    def __init__(
        self,
        settings: Settings,
        session_id: str | None = None,
        *,
        provider: LLMProvider | None = None,
        generation: GenerationChoice | None = None,
        web: bool = True,
        web_search: bool = False,
    ) -> None:
        # 单轮最大输出是**运行期可改**的（设置 → 常规，ADR-101）：
        # `.env` 只是种子，`data/settings.json` 里的值优先。在这里统一折算一次，
        # 循环、窗口预算、provider 都从 `self.settings` 读，不会两处读到不同的数。
        self.settings = replace(
            settings,
            max_output_tokens=max_output_tokens_for(
                settings.data_dir, settings.max_output_tokens
            ),
        )
        self.generation = generation
        self._web_enabled = web
        # 会话级的"联网搜索"开关（默认关）。
        # **它只管 web_search**：web_open_url 是"读用户递过来的东西"，
        # 不该因为用户没开搜索就用不了。
        self.web_search_on = bool(web_search)
        # 上限是**运行期可改**的（data/memory.json），所以只能从这里拿 ——
        # 自己拼 max_items=settings.memory_limit 会在用户改过上限之后失效（ADR-057）。
        self.memory = open_memory(settings)
        # 从这里往后任何一步抛异常，都不能把已经打开的 MemoryStore 丢在那儿
        # （SQLite 句柄不关会让临时目录都删不掉，测试里踩过）。
        try:
            self.tool_store = ToolStore(settings.data_dir / TOOLS_FILE)
            self.web_config = self.tool_store.web()
            # 背景档案（dream memory）：**只读**地接进来。
            # 读操作不会创建目录，所以"全新机器上看一眼"不会在 data/ 里留下空壳。
            self.vault = VaultStore(
                settings.vault_dir,
                read_max_chars=settings.vault_read_max_chars,
                write_max_chars=settings.vault_write_max_chars,
                max_files=settings.vault_max_files,
                name_max_chars=settings.vault_name_max_chars,
                readme_token_budget=settings.vault_readme_token_budget,
            )
            # 一处判断，两处使用：**注册工具**与**提示词里说不说有这回事**
            # 必须完全一致（见 context.vault_prompt_text）。
            # 空 vault 就什么都不注册：全新机器上不该凭空多出三个用不到的工具定义。
            self.vault_available = self.vault.has_content()
            readme, _ = self.vault.readme_text() if self.vault_available else ("", False)
            self.vault_text = (
                vault_prompt_text(readme, available=True)
                if self.vault_available
                else ""
            )
            self.registry = ToolRegistry(max_result_chars=settings.max_tool_result_chars)
            for tool in (
                MemCreateTool(self.memory),
                MemEditTool(self.memory),
                MemDeleteTool(self.memory),
                # 时间工具无条件注册：只读、不花钱、无依赖，随时能告诉模型"现在几点"。
                # 时区是运行期可改的设置（「设置 → 常规」），改完下一轮对话就生效。
                TimeTool(offset_hours=timezone_for(settings.data_dir)),
            ):
                self.registry.register(tool)
            if self.vault_available:
                for tool in (
                    VaultListTool(
                        self.vault, max_result_chars=settings.max_tool_result_chars
                    ),
                    VaultReadTool(
                        self.vault, max_result_chars=settings.max_tool_result_chars
                    ),
                    VaultSearchTool(
                        self.vault, max_result_chars=settings.max_tool_result_chars
                    ),
                ):
                    self.registry.register(tool)
            # 联网工具只给**对话** agent。dream 是离线巩固，让它上网既没必要也更危险
            # （它会拿着网页内容去改写记忆库）。
            #
            # 这里按"这个会话开没开搜索"决定注册哪些工具：
            # 没开搜索时模型**连工具定义都看不到** —— 比只在提示里劝阻靠得住。
            if web:
                if self.web_search_on and self.web_config.search_available:
                    self.registry.register(WebSearchTool(self.web_config))
                if self.web_config.enabled:
                    # 把"工具结果的通用上限"显式传进去：抓取正文的有效上限是
                    # min(工具设置里的 max_fetch_chars, 这个值)。
                    # 早先两者各说各话 —— 设置里写 8000，实际被 2000 静默砍掉，
                    # 模型既看不到原因，界面上的数字也是假的。
                    self.registry.register(
                        WebOpenUrlTool(
                            self.web_config,
                            max_result_chars=settings.max_tool_result_chars,
                        )
                    )
            self.provider = provider or OpenAICompatProvider(
                self.settings,
                label=t("ui.runner.default_label"),
                send_reasoning_content=True,
            )
            self.log: EventLog | None = None
            self.session_id = ""
            self.loop: AgentLoop
            self.open_session(session_id)
        except BaseException:
            self.memory.close()
            raise

    def open_session(self, session_id: str | None = None) -> None:
        if self.log is not None:
            self.log.close()
        self.session_id = session_id or new_session_id()
        self.log = EventLog(
            self.settings.sessions_dir / f"{self.session_id}.jsonl", self.session_id
        )
        self.log.append(
            "session_start",
            model=self.settings.model,
            reasoning_effort=self.settings.reasoning_effort,
            provider=(self.generation.provider_id if self.generation else ""),
            tools=self.registry.names(),
        )
        self.loop = AgentLoop(
            provider=self.provider,
            registry=self.registry,
            settings=self.settings,
            memory=self.memory,
            log=self.log,
            web_tools_text=(
                web_tools_text(
                    enabled=self.web_config.enabled,
                    search_available=self.web_config.search_available,
                    search_on=self.web_search_on,
                )
                if self._web_enabled
                else ""
            ),
            vault_text=self.vault_text,
        )

    def close(self) -> None:
        if self.log is not None:
            self.log.close()
        self.memory.close()

    def __enter__(self) -> "Runtime":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
